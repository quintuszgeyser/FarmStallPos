# 🚀 Quick Start - Do This Now

## Step 1: Copy Face Models
```powershell
git pull origin main
.\copy_face_models.ps1
```

## Step 2: Watch Logs
```powershell
Get-Content logs\recognition_service.log -Tail 20 -Wait
```

## Step 3: Walk In Front of Indoor Camera

You should see:
```
[INFO] Extracted attributes: height=XXXcm, hair=X, gender=X...
[INFO] Auto-enrolling new customer (signals=2, physical=True)
[INFO] Created customer CUST-0002
[INFO] Enrolled face
[INFO] Enrolled gait
[INFO] Stored physical attributes
```

## That's It!

Everything else happens automatically:
- ✅ Customer identified on return visits
- ✅ Till badge appears when at checkout (if name added)
- ✅ Purchases linked automatically
- ✅ Sessions created every 5 minutes (dwell time)

---

**For full details:** See `WHILE_YOU_WERE_AWAY.md`

**Having issues?** 
1. Check `dir "C:\Users\Quintusz\.insightface\models\buffalo_l"` has files
2. Check Recognition service logs for errors
3. Verify Flask QA is running on http://127.0.0.1:5000 (Prod uses HTTPS on 5443)
