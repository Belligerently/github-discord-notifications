"""Poll public GitHub contributions and deliver changed summaries to Discord."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class APIError(RuntimeError):
    pass


def request_json(url, *, token=None, payload=None):
    headers = {"User-Agent": "github-discord-monitor", "Accept": "application/json"}
    if token:
        headers["Accept"] = "application/vnd.github+json"
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:
                time.sleep(min(float(error.headers.get("Retry-After", "5")), 30))
                continue
            # Do not include URLs: the Discord URL contains its credential.
            raise APIError(f"API request failed (HTTP {error.code})") from None
        except (urllib.error.URLError, TimeoutError):
            raise APIError("API request failed (network error)") from None
    raise APIError("API request failed after retries")


class GitHub:
    def __init__(self, token):
        self.token = token

    def get(self, path):
        return request_json("https://api.github.com" + path, token=self.token)

    def pages(self, path, field=None):
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            data = self.get(f"{path}{separator}per_page=100&page={page}")
            rows = data[field] if field else data
            yield from rows
            if len(rows) < 100:
                return
        raise APIError("Pagination limit reached; state has not been advanced")

    def contributions(self, username):
        # Include every open item and closed items updated within the last 90 days.
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).date()
        found = {}
        for suffix in ("is:open", f"is:closed updated:>={cutoff}"):
            query = urllib.parse.quote(f"author:{username} is:public {suffix}")
            path = f"/search/issues?q={query}&sort=updated&order=desc"
            first = self.get(path + "&per_page=100&page=1")
            if first.get("incomplete_results") or first["total_count"] > 1000:
                raise APIError("GitHub search is incomplete; refusing to miss updates")
            rows = first["items"]
            for page in range(2, (first["total_count"] + 99) // 100 + 1):
                data = self.get(path + f"&per_page=100&page={page}")
                if data.get("incomplete_results"):
                    raise APIError("GitHub search is incomplete")
                rows.extend(data["items"])
            for item in rows:
                found[item["html_url"]] = item
        return list(found.values())

    def snapshot(self, item):
        repo = item["repository_url"].split("/repos/", 1)[1]
        base = f"/repos/{repo}"
        snap = {
            "title": item["title"], "state": item["state"],
            "labels": sorted(label["name"] for label in item["labels"]),
            "assignees": sorted(user["login"] for user in item["assignees"]),
            "comments": item["comments"], "updated_at": item["updated_at"],
            "kind": "PR" if "pull_request" in item else "Issue",
        }
        if "pull_request" not in item:
            return snap
        pr = self.get(f"{base}/pulls/{item['number']}")
        snap.update(state="merged" if pr["merged_at"] else pr["state"],
                    draft=pr["draft"], head=pr["head"]["sha"],
                    review_comments=pr["review_comments"])
        reviews = self.pages(f"{base}/pulls/{item['number']}/reviews")
        snap["reviews"] = sorted([review["id"], review["state"], review["submitted_at"]]
                                 for review in reviews)
        sha = pr["head"]["sha"]
        status = self.get(f"{base}/commits/{sha}/status")
        # Ignore empty check lists; surface each named check's current result.
        checks = self.pages(f"{base}/commits/{sha}/check-runs", "check_runs")
        latest = {}
        for check in checks:
            key = f"{check['app']['id']}:{check['name']}"
            if key not in latest or check["id"] > latest[key]["id"]:
                latest[key] = check
        snap["checks"] = sorted([key, c["conclusion"] or c["status"]]
                                 for key, c in latest.items())
        snap["status"] = sorted([s["context"], s["state"]] for s in status["statuses"])
        return snap


def changes(old, new):
    messages = []
    for key, label in (("state", "State"), ("labels", "Labels"), ("assignees", "Assigned")):
        if old.get(key) != new.get(key):
            value = new[key]
            messages.append(f"{label}: {', '.join(value) if isinstance(value, list) else value}".rstrip() or label)
    if old.get("comments") != new.get("comments"):
        messages.append("Issue/PR comments changed")
    if old.get("reviews") != new.get("reviews"):
        messages.append("PR reviews changed")
    if old.get("review_comments") != new.get("review_comments"):
        messages.append("Inline review comments changed")
    if old.get("head") != new.get("head"):
        messages.append("PR branch updated")
    if old.get("draft") != new.get("draft"):
        messages.append("Draft" if new.get("draft") else "Ready for review")
    if old.get("checks") != new.get("checks") or old.get("status") != new.get("status"):
        results = [f"{name.split(':', 1)[-1]}: {state}" for name, state in new.get("checks", [])]
        results += [f"{name}: {state}" for name, state in new.get("status", [])]
        messages.append("CI: " + (", ".join(results)[:800] or "checks changed"))
    if not messages and old.get("updated_at") != new.get("updated_at"):
        messages.append("Issue/PR updated")
    return messages


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def discord_sender(webhook, role_id=""):
    if not re.fullmatch(r"https://discord\.com/api/(?:v\d+/)?webhooks/\d+/[\w.-]+", webhook):
        raise ValueError("Expected a Discord channel webhook URL")
    if not role_id.isdigit():
        raise ValueError("Discord role ID must contain digits only")

    def send(title, description, url=None):
        embed = {"title": title[:256], "description": description[:3500], "color": 5793266}
        if url:
            embed["url"] = url
        request_json(webhook + "?wait=true", payload={
            "content": f"<@&{role_id}>",
            "allowed_mentions": {"parse": [], "roles": [role_id]},
            "embeds": [embed],
        })
    return send


def ntfy_sender(topic):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", topic):
        raise ValueError("Expected a long random ntfy topic")

    def send(title, description, url=None, *, kind):
        payload = {
            "topic": topic, "title": title[:256], "message": description[:3500],
            "priority": 4,
            "tags": ["twisted_rightwards_arrows" if kind == "PR" else "speech_balloon"],
        }
        if url:
            payload["click"] = url
        request_json("https://ntfy.sh/", payload=payload)
    return send


def run(client, send, path):
    state = json.loads(path.read_text()) if path.exists() else {"version": 1, "items": {}, "initialized": False}
    if state.get("version") != 1 or not isinstance(state.get("items"), dict):
        raise ValueError("Unrecognized monitor state; preserve it for recovery")
    username = os.environ.get("GH_USERNAME", "Belligerently")
    if not re.fullmatch(r"[A-Za-z0-9-]+", username):
        raise ValueError("Invalid GitHub username")
    initialized = state.get("initialized", False)
    # Fetch all snapshots before sending anything, so an API failure cannot make
    # a partial snapshot look like a state change or reset the baseline.
    snapshots = []
    for item in client.contributions(username):
        url = item["html_url"]
        previous = state["items"].get(url)
        if (item.get("state") == "closed" and previous is not None
                and previous.get("updated_at") == item.get("updated_at")):
            current = previous
        else:
            current = client.snapshot(item)
        snapshots.append((url, current))
    for url, new in snapshots:
        old = state["items"].get(url)
        detail = changes(old, new) if old is not None else [f"New {new['kind']}: {new['state']}"]
        if initialized and detail:
            # Titles/labels are untrusted text, never commands or allowed mentions.
            send(f"{new['kind']}: {new['title']}", "\n".join(detail), url, kind=new["kind"])
        state["items"][url] = new
        save_state(path, state)
    state["initialized"] = True
    state["last_successful_check_date"] = dt.datetime.now(dt.timezone.utc).date().isoformat()
    save_state(path, state)
    print(f"Checked {len(snapshots)} public contributions for {username}; baseline={'existing' if initialized else 'created'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Send a connection test without changing the baseline")
    parser.add_argument("--state", default="state/snapshot.json")
    parser.add_argument("--ntfy-only", action="store_true", help="Use ntfy with its own delivery state")
    args = parser.parse_args()
    if args.ntfy_only:
        send = ntfy_sender(os.environ["NTFY_TOPIC"])
        if args.test:
            send("GitHub watch notifications connected", "Your public GitHub updates will appear here.", kind="Issue")
        else:
            run(GitHub(os.environ.get("GITHUB_TOKEN")), send, Path(args.state))
        return
    routes = {
        "Issue": discord_sender(os.environ["ISSUES_WEBHOOK_URL"], os.environ["ISSUES_ROLE_ID"]),
        "PR": discord_sender(os.environ["PRS_WEBHOOK_URL"], os.environ["PRS_ROLE_ID"]),
    }
    def send(title, description, url=None, *, kind):
        routes[kind](title, description, url)
    if args.test:
        for kind, sender in routes.items():
            sender(f"GitHub {kind} notifications connected", f"Updates to your public {kind.lower()}s will appear here.")
        return
    run(GitHub(os.environ.get("GITHUB_TOKEN")), send, Path(args.state))


if __name__ == "__main__":
    try:
        main()
    except (APIError, ValueError, KeyError) as error:
        # Report the error class without secret-bearing HTTP URLs or payloads.
        raise SystemExit(f"Monitor stopped: {type(error).__name__}. Check configuration/API availability.") from None
