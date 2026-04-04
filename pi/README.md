# Berry Raspberry Pi Setup

## Installation (2 steps)

### 1. Install Raspberry Pi OS
- Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
- Choose "Raspberry Pi OS Lite (64-bit)"
- Click **⚙️ Settings**:
  - Hostname: your choice (e.g. `berry`)
  - Username: your choice (e.g. `berry`), password of your choice
  - Configure WiFi
  - Enable SSH
- Flash to SD card

### 2. Install Berry
```bash
ssh <your-user>@<your-hostname>.local
curl -sSL https://raw.githubusercontent.com/emieljanson/berry/main/install.sh | bash
sudo reboot
```

**Done!** 🎉

---

## First Time Setup

After reboot, Berry shows a setup screen:

1. Open **Spotify** on your phone
2. Tap the **speaker icon** (bottom left)
3. Select **"Berry"** from the list
4. Berry is now connected! 🎵

---

## WiFi Setup

Berry automatically handles WiFi issues:

- **Has WiFi?** → Berry starts normally
- **No WiFi?** → Berry creates a hotspot **"Berry-Setup"**

To configure WiFi:
1. Connect your phone to **"Berry-Setup"** hotspot
2. A browser opens automatically
3. Select your WiFi network
4. Done! Berry connects and starts

---

## What the install script does

- ✅ Installs go-librespot (Spotify Connect)
- ✅ Installs Python dependencies (Pygame, Pillow, etc.)
- ✅ Installs WiFi Connect (captive portal)
- ✅ Configures auto-start on boot
- ✅ Configures auto-updates (nightly at 03:00)
- ✅ Starts Berry

---

## Management

### Services
```bash
sudo systemctl status berry-native      # Status
sudo systemctl restart berry-native     # Restart
journalctl -u berry-native -f           # Logs
```

### Manual update
```bash
cd ~/berry && git pull
source venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart berry-native
```

### Update logs
```bash
cat ~/berry-update.log
```

### Analytics (stable distinct id)
If you use PostHog and want one fixed device identity, set this once:

```bash
cd ~/berry
cp .env.example .env  # if .env doesn't exist yet
nano .env
```

Recommended values in `.env`:

```bash
ANALYTICS_DISTINCT_ID=berry-livingroom
ANALYTICS_INCLUDE_CONTENT=0
ANALYTICS_USE_MACHINE_ID=0
```

Then restart Berry:

```bash
sudo systemctl restart berry-native
```

### Analytics for multiple Raspberry Pi devices
Use one of these setups:

- **Unique per Pi (recommended for your setup)**  
  Keep `ANALYTICS_DISTINCT_ID` empty and enable machine id:

  ```bash
  ANALYTICS_DISTINCT_ID=
  ANALYTICS_USE_MACHINE_ID=1
  ```

  Result: each Raspberry Pi shows up as its own device in PostHog.

- **One fixed id (only for shared demo devices)**  
  Set one explicit id and disable machine id:

  ```bash
  ANALYTICS_DISTINCT_ID=berry-demo
  ANALYTICS_USE_MACHINE_ID=0
  ```

  Result: multiple installs report as one logical device id.
