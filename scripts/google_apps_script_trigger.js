/**
 * Reliable hourly trigger for the Deal Hunter workflow (free).
 *
 * GitHub's own cron schedule often starts late or skips runs. This Google Apps
 * Script runs every hour from your Google account and asks GitHub to start the
 * workflow. The GitHub cron stays as a backup; duplicate runs are harmless
 * (the email still goes out at most every 4 hours).
 *
 * Setup (once):
 *  1. GitHub → Settings → Developer settings → Personal access tokens →
 *     Fine-grained tokens → Generate new token
 *       - Repository access: Only select repositories → black-friday-hunter
 *       - Permissions → Repository permissions → Actions: Read and write
 *       - Expiration: up to 1 year (set a reminder to renew it)
 *  2. https://script.google.com → New project → paste this file.
 *  3. Project Settings (gear) → Script properties → Add property
 *       GITHUB_TOKEN = the token from step 1
 *  4. Triggers (alarm clock) → Add trigger → function: triggerDealHunter,
 *     event source: Time-driven, type: Hour timer, Every hour → Save
 *     (Google asks you to authorize the script once.)
 *  5. Run triggerDealHunter once manually to test: a new run appears in the
 *     repository's Actions tab within a few seconds.
 */
const REPO = 'vahehakobyanv/black-friday-hunter';
const WORKFLOW = 'hourly_deals.yml';

function triggerDealHunter() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Script property GITHUB_TOKEN is not set');
  const url = 'https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW + '/dispatches';
  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ ref: 'main', inputs: { send_email: 'true' } }),
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() !== 204) {
    throw new Error('GitHub answered ' + res.getResponseCode() + ': ' + res.getContentText());
  }
}
