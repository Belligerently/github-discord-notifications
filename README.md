# GitHub Discord notifications

Watches public issues and pull requests authored by Belligerently. Issue updates go to the issues webhook; PR updates go to the PR webhook. Each message mentions only its designated role.

The workflow checks roughly every five minutes. GitHub schedules may be delayed. The first successful run builds a quiet baseline; later runs report comments, labels, assignments, reviews, branch changes, CI results, and closed/merged state. Closed items updated in the past 90 days are included. Changes that happen and revert between checks may not be observed.

## Configuration

Add two repository Actions secrets: `ISSUES_WEBHOOK_URL` and `PRS_WEBHOOK_URL`. Role IDs and the GitHub username are configured in `.github/workflows/notify.yml`. No personal GitHub token is needed: the workflow uses its automatic token and public API reads.

Run **GitHub to Discord** manually with **Send a test notification** selected to check both channels and role pings. Normal scheduled runs only send updates.

The `monitor-state` branch stores public snapshots and the date of the last successful check. It is updated after confirmed deliveries and successful daily checks. No webhook credentials or private repository content are committed. The workflow's write permission is used only to persist this state in this repository.

## Operations

Check the Actions page for failures. GitHub can disable scheduled workflows in public repositories after 60 days without repository activity; verify the schedule remains enabled if you stop seeing runs. This monitor records its real operating state as repository activity.

To stop notifications, disable the workflow in Actions. To revoke a destination, delete its Discord webhook or remove its Actions secret.

## Tests

Run `python3 -m unittest -v`. The monitor uses only the Python standard library.

Delivery is best effort: a crash after Discord accepts a message but before state is pushed can produce a duplicate on retry. API/search failures stop the run rather than silently dropping updates.


## Phone and Pixel Watch alerts with ntfy

Subscribe in the ntfy Android app to a long, random topic on https://ntfy.sh. Store that topic in the repository Actions secret `NTFY_TOPIC`; never commit it. Enable Android notification permission for ntfy, and enable ntfy in the Pixel Watch companion app's notification settings.

The monitor sends high-priority ntfy notifications with a link to the GitHub item. The ntfy topic receives both issue and PR updates. Discord retains separate channels and role mentions.

ntfy and Discord each have their own delivery snapshots on the `monitor-state` branch. A delivery failure in one does not prevent the other from advancing; the failed destination retries on a later run. The first ntfy run creates a quiet baseline.

For a connection test, run the workflow with **Send a phone/watch test through ntfy only** selected. Public ntfy topics are unprotected: anyone who knows the random topic can read or publish to it. The monitor sends only public GitHub metadata.
