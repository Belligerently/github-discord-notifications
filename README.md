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

