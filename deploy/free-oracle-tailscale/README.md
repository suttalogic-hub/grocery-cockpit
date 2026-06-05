# Free Oracle + Tailscale path

This is the no-monthly-cost path for making Grocery Cockpit independent of the laptop.

## What we are building

- Oracle Cloud Always Free Ubuntu VM: always-on app/scanner machine.
- Tailscale Personal: private iPhone-to-server network.
- Tailscale Serve: HTTPS Home Screen URL inside your private tailnet.
- Virtual display + noVNC: one-time grocery app logins on the server.

After this works, the laptop can be off.

Official references:

- Oracle Always Free resources: https://docs.oracle.com/iaas/Content/FreeTier/resourceref.htm
- Tailscale pricing / Personal plan: https://tailscale.com/pricing

Stay inside the Always Free labels and do not upgrade or add paid resources unless you intentionally choose to later.

## What you must do manually

These steps involve your accounts, OTPs, or payment verification, so they should be done by you:

1. Create/sign in to Oracle Cloud.
2. Create an Always Free-eligible Ubuntu VM.
3. Install Tailscale on your iPhone and log in.
4. Log in to each grocery provider once through the server setup browser.

## VM recommendation

Use Ubuntu 24.04 if available. Pick an Always Free-eligible Ampere A1 shape if Oracle has capacity in your region. The AMD micro shape is usually too small for browser scanning.

Keep the boot volume and shape inside Oracle's Always Free limits.

## After the VM exists

From your laptop, create a migration bundle:

```powershell
cd "C:\Users\paart\OneDrive\Documents\New project\grocery-cockpit"
.\prepare_free_vm_bundle.ps1
```

For your own private migration only, you can include personal config and price history:

```powershell
.\prepare_free_vm_bundle.ps1 -IncludePersonalData
```

Upload the generated zip from `dist\` to the VM, then on the VM:

```bash
sudo mkdir -p /opt/grocery-cockpit
sudo unzip grocery-cockpit-free-vm-*.zip -d /opt/grocery-cockpit
sudo chown -R "$USER:$USER" /opt/grocery-cockpit
cd /opt/grocery-cockpit
bash deploy/free-oracle-tailscale/bootstrap_server.sh
```

Join the server to your Tailscale account:

```bash
sudo tailscale up --ssh --hostname grocery-cockpit
```

Publish the private HTTPS app URL:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8877
sudo tailscale serve --bg --https=8443 http://127.0.0.1:6080
tailscale serve status
```

Open the HTTPS dashboard URL from your iPhone while Tailscale is connected, then add it to Home Screen.

## Grocery provider login

Open the noVNC URL from `tailscale serve status`, usually the same Tailscale hostname with port `8443`.

Then in Grocery Cockpit:

1. Menu
2. Connected Apps
3. Setup for one provider
4. Use the noVNC browser window to log in and set your own delivery location
5. Repeat for all providers

The browser profiles are stored on the VM under:

```text
/opt/grocery-cockpit/data/browser-profiles
```

## Why this is private

Tailscale Serve shares the app inside your tailnet. It is not a random public Cloudflare quick tunnel. Only devices logged in to your Tailscale account can reach it.

## If Oracle free capacity is unavailable

Do not switch to paid by accident. Try another Oracle region first. If Oracle remains unavailable, the fallback is a low-cost VPS, but that is outside the no-monthly-cost target.
