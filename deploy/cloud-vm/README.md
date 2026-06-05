# Laptop-free deployment

This is the path for making Grocery Cockpit independent of the laptop.

## Target shape

- A small always-on Ubuntu server runs the dashboard.
- The SQLite database and browser profiles live on that server under `/opt/grocery-cockpit/data`.
- The iPhone opens a stable HTTPS URL that points to the server.
- The scanner runs on the server, not the laptop.

## What changes compared with the current setup

The temporary Cloudflare quick tunnel goes away. The laptop no longer has to stay awake.

The hard part is logged-in grocery access. Zepto, Blinkit, Swiggy Instamart, Amazon Now, JioMart, DMart, and BigBasket currently depend on browser sessions. For full independence, each provider needs to be logged in once on the cloud scanner machine, with the delivery location set to the user's own serviceable address or pincode.

## Server setup outline

1. Create an Ubuntu server.
2. Point a stable HTTPS domain or tunnel at that server.
3. Copy this project to `/opt/grocery-cockpit`.
4. Copy `config.json` and `data/grocery.sqlite` from the laptop into `/opt/grocery-cockpit`.
5. Install the system packages:

```bash
cd /opt/grocery-cockpit
bash deploy/cloud-vm/install_ubuntu.sh
```

6. Install Node dependencies:

```bash
cd /opt/grocery-cockpit
npm ci --omit=dev
```

7. Install the service:

```bash
sudo cp deploy/cloud-vm/grocery-cockpit.service /etc/systemd/system/grocery-cockpit.service
sudo systemctl daemon-reload
sudo systemctl enable --now grocery-cockpit
sudo systemctl status grocery-cockpit
```

8. Put HTTPS in front of port `8877`.

Recommended options:

- a normal domain with Caddy/Nginx reverse proxy
- a Cloudflare named tunnel on a domain you control
- Tailscale Serve/Funnel if you want private access first

For a Caddy reverse proxy, copy `Caddyfile.example`, replace `groceries.example.com`
with the real domain, and point it at `127.0.0.1:8877`.

## Provider login reality

Do not assume the Windows browser profiles can simply be copied to Linux. Cookies are often encrypted by the operating system, so provider logins may not survive the move.

Plan to log in again on the cloud scanner for:

- Zepto
- Blinkit
- Swiggy Instamart
- Amazon Now
- JioMart
- DMart Ready
- BigBasket

Once these sessions exist on the server, the laptop is no longer part of the price-checking loop.

## Data to migrate

Minimum:

- `config.json`
- `data/grocery.sqlite`

Useful history/status:

- `data/*_probe_results.json`
- `data/auto_scan_status.json`
- `data/basket_scan_status.json`

Usually do not migrate:

- temporary tunnel logs
- `tools/cloudflared.exe`
- Windows-specific browser profiles

## Docker option

The included `Dockerfile` runs the dashboard and installs Chromium. Use a volume for `/app/data` so history survives container restarts.

Example:

```bash
docker build -t grocery-cockpit .
docker run -d --name grocery-cockpit \
  -p 8877:8877 \
  -v /opt/grocery-cockpit-data:/app/data \
  -v /opt/grocery-cockpit-config.json:/app/config.json:ro \
  grocery-cockpit
```

Docker is good for the dashboard and headless checks. Provider login/setup is easier on a plain VM until a remote browser setup flow is added.
