Note: This project has bugs that are currently being worked on. I'll get around to it when I have time. Alternately, feel free to use/improve. MIT-license.

# XDC Epoch Notifier

**Daily XDC Masternode Epoch Rewards Notifier**  
Automatically checks your XDC masternode owner address(es) every day at 08:00 AEST and sends a complete report to your personal ntfy.sh channel.

Even when rewards are zero, it sends a notification so you always know the script is running (daily heartbeat).

For more projects, visit [XDC Outpost](https://s4njk4n.github.io/XDCOutpost/).

## Free Open-Source Version

For tech-savvy users: Fork and self-host in GitHub Actions. No cost, full control.

### What You Need First

1. **Etherscan V2 API Key**  
   - What is it? A free API key that lets the script pull transaction data.  
   - Why needed? To accurately calculate epoch rewards using balance changes + tx analysis (same method as XDC_Masternode_Rewards_Calculator).  
   - How to get one (free):  
     a. Go to [https://xdcscan.com/register](https://xdcscan.com/register) or [https://etherscan.io/register](https://etherscan.io/register).  
     b. Sign up with email/password.  
     c. Go to API Keys section.  
     d. Click "Add" and name it (e.g. "XDC Epoch Notifier").  
     e. Copy the key.

2. **ntfy.sh for Notifications**  
   - Free push notification service (no account needed).  
   - Download the app: [iOS](https://apps.apple.com/us/app/ntfy/id1625396347) or [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).  
   - Create a unique topic (e.g. `xdc-rewards-mysecret123`).  
   - Subscribe in the app so you receive alerts.

3. **Your Node Details (CSV)**  
   Make a CSV with one line per masternode:

```
0xYourMasternodeOwnerAddress1,ntfy-topic-name-1,YourEtherscanV2APIKey1
0xYourMasternodeOwnerAddress2,ntfy-topic-name-2,YourEtherscanV2APIKey2
```

- `owner_address`: Your masternode owner address (0x...)  
- `ntfy_topic`: Your ntfy.sh topic  
- `etherscan_v2_api_key`: Your API key for that address

### Quick Setup

1. **Fork this repo** (top-right button).  
2. **Enable workflows** (Actions tab → enable if prompted).  
3. **Add the secret**:  
- Go to **Settings** → **Secrets and variables** → **Actions** → **New repository secret**  
- **Name**: `XDC_REWARDS_CSV`  
- **Value**: Paste your full CSV (no header line).  
4. **Add the files** (already included when you fork):  
- `xdc_epoch_rewards.sh` (main script — keep executable)  
- `.github/workflows/xdc-rewards-notifier.yml` (daily schedule)  
5. **Commit & push** — the daily check starts automatically at 08:00 AEST.

**Security Notes**:  
Never commit secrets to code. All sensitive data stays in GitHub Secrets (encrypted). Public forks are safe.

**Debug Tips**:  
- Check Actions logs for any errors.  
- No notification? Verify your ntfy topic in the app.

Runs free on GitHub runners.

## Example Notification

```
 XDC Masternode Daily Rewards CheckDate: 2026-03-21 (08:00 AEST)
Address: 0x1234...
Epoch rewards received: 124.5678 XDC Daily monitoring check completed successfully
```


## Related Projects
- [XDC_Masternode_Rewards_Calculator](https://github.com/s4njk4n/XDC_Masternode_Rewards_Calculator) — Web-based rewards calculator  
- [XDC_Tycoon](https://github.com/s4njk4n/XDC_Tycoon) — Masternode status + reward top-up alerts  
- [XDC_Sentinel](https://github.com/s4njk4n/XDC_Sentinel) — Node uptime monitoring

---

Made with ❤️ for the XDC community.  
Use at your own risk. No warranties provided.
