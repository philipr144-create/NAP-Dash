A standalone local web server and dashboard that runs directly on your comma device. It provides a massive 3D instrument cluster, an interactive dashcam viewer with HUD exporting, and live control over Openpilot settings—including an instant Cruise Speed Trim.

✨ Features
3D Live Cluster: A massive, edge-to-edge 3D physics grid displaying steering angle, 3D lead car tracking, and pedal inputs.
Live Speed Trim: Bump your cruise target speed up or down (+1/+5) instantly from your phone or PC browser.
Dashcam Viewer & Exporter: Browse previous drives, watch camera feeds (Road, Wide, Driver), and export MP4 clips with the Openpilot telemetry HUD burned directly into the video.
On-the-Fly Toggles: Change your Follow Distance, Driving Personality, and Experimental Mode without navigating through the comma UI.
Safe Automated Installer: The installation script automatically backs up your stock Openpilot files before injecting the custom logic, allowing for a completely clean uninstall at any time.

⚙️ How It Works
The dashboard runs a lightweight Python web server (server_v21.py) on port 7070 of your comma device.
Settings Bridge: When you adjust a setting (like Speed Trim) in the browser, the server writes it to /data/nap_settings.json.
CarState Injection: The installer patches carstate.py to read this JSON file dynamically, injecting the speed offset directly into Openpilot's cruise target without needing a reboot.
Auto-Boot: The server is safely injected into process_config.py so it starts up automatically alongside Openpilot.

📱 Usage
Once your comma device has fully booted, connect your phone or PC to the comma's Wi-Fi hotspot.

Open your web browser and navigate to:
http://192.168.xxx.1:7070
If you are accessing it over your home Wi-Fi network instead of the comma's hotspot, replace 192.168.xxx.1 with the local IP address of your comma device).


Uninstallation / Restore
If you want to remove the server or revert your Openpilot installation back to completely stock Not Auto Pilot 11.1, the installer has a built-in rollback feature. It will restore the .bak files created during the initial installation.

SSH into your comma device and run:cd /data/openpilot
python3 install_nap.py --restore
sudo reboot
