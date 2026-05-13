# SSH Setup for Remote Log Monitoring

This guide sets up SSH on the Mini PC so Claude can monitor logs remotely.

## On the Mini PC

### Step 1: Run Setup Script as Administrator

```powershell
# Right-click PowerShell, "Run as Administrator"
cd C:\Users\Quintusz\farm_pos_web
powershell -ExecutionPolicy Bypass -File setup_ssh.ps1
```

The script will:
1. Install OpenSSH Server (if needed)
2. Start SSH service and set to auto-start
3. Configure Windows Firewall
4. Display your connection info
5. Set PowerShell as the default shell

### Step 2: Note Your Connection Info

The script will show:
- **Computer name**: e.g., `MINIPC`
- **Username**: e.g., `Quintusz`
- **Tailscale IP**: e.g., `100.86.32.13`

## On Your Dev Machine (This PC)

### Test SSH Connection

```bash
# Replace with your actual username and IP
ssh Quintusz@100.86.32.13
```

First time:
1. Type `yes` to accept host key
2. Enter your Windows password

### Monitor Logs Remotely

```bash
# POS logs (live tail)
ssh Quintusz@100.86.32.13 'Get-Content C:\Users\Quintusz\farm_pos_web\logs\pos.log -Tail 50 -Wait'

# Recognition service logs
ssh Quintusz@100.86.32.13 'Get-Content C:\Users\Quintusz\farm_pos_web\logs\recognition_service.log -Tail 50 -Wait'

# Deploy logs
ssh Quintusz@100.86.32.13 'Get-Content C:\Users\Quintusz\farm_pos_web\logs\deploy.log -Tail 20'

# All recent errors (last 5 minutes)
ssh Quintusz@100.86.32.13 'Get-Content C:\Users\Quintusz\farm_pos_web\logs\*.log | Select-String -Pattern "ERROR|Exception|Traceback" | Select-Object -Last 20'
```

### One-Line Status Check

```bash
ssh Quintusz@100.86.32.13 'cd C:\Users\Quintusz\farm_pos_web; powershell -File test_full_system.ps1'
```

## For Claude Code

Once SSH is set up, Claude can run commands like:

```bash
# Check if services are running
ssh Quintusz@100.86.32.13 'Get-Service FarmPOS-qa,FarmPOS-Recognition | Format-Table -AutoSize'

# View last 100 lines of POS log
ssh Quintusz@100.86.32.13 'Get-Content C:\Users\Quintusz\farm_pos_web\logs\pos.log -Tail 100'

# Check database connection
ssh Quintusz@100.86.32.13 'cd C:\Users\Quintusz\farm_pos_web; .venv\Scripts\python.exe check_db_connection.py'

# Restart services
ssh Quintusz@100.86.32.13 'Restart-Service FarmPOS-qa,FarmPOS-Recognition'
```

## Troubleshooting

### "Connection refused"
- Check SSH service: `Get-Service sshd`
- Should show "Running"
- If stopped: `Start-Service sshd`

### "Permission denied"
- Make sure you're using the correct Windows username
- Password is your Windows login password

### "Host key verification failed"
- Remove old key: `ssh-keygen -R 100.86.32.13`
- Try connecting again

### Firewall blocking
- Check rule exists: `Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP"`
- If not found, run setup script again

## Security Notes

- SSH only accessible via Tailscale network (100.x.x.x range)
- Not exposed to internet
- Uses your Windows password for authentication
- Can set up SSH keys later for passwordless login if needed

## Next Steps After Setup

1. Test connection from dev machine
2. Clone the repo on dev machine (if not already)
3. Make changes locally
4. Push to GitHub
5. Claude monitors Mini PC logs via SSH
6. Pull changes on Mini PC when ready
