# Optional n8n installation

Mikachan offers n8n during the one-line `v2` installation and later under **Integrations → Independent n8n installer / manager**. The two applications remain independent. The bot uses `/opt/Mikachan` and its systemd unit. n8n uses `/opt/n8n`, Docker Compose project `mikachan_n8n`, its own SQLite data, encryption key, backups, Caddy files, and `/usr/local/bin/n8n-manage`. Bot setup and AI credentials are not required to install n8n. Installing n8n does not configure or replace an n8n workflow for Mikachan.

## First-install choices

The installer shows a read-only host preflight, installs Mikachan first, and asks **Install n8n on this server?** Choose **No, Mikachan Only** to finish without Docker or n8n. Choose **Yes, Install n8n** to select one of three browser access methods:

1. **I Have a Domain.** Create an A record pointing to the displayed public VPS IPv4. An existing AAAA record must match the VPS public IPv6. A DuckDNS hostname works here too. The installer checks public DNS before changing n8n. It never asks for a DNS account password or DuckDNS token. A wrong record offers retry, repair instructions, a different hostname, or private access.
2. **Get Free Address.** Register a free DuckDNS subdomain yourself at <https://www.duckdns.org/>, point it to the displayed VPS IP, then enter the full hostname. The installer does not register or update DuckDNS on your behalf. If the VPS IP changes, update DuckDNS manually.
3. **Use Server IP.** The bundled configuration does not assume a trusted, renewable IP-address certificate. It configures private access on `127.0.0.1:5678`. On **your computer**, run `ssh -N -L 5678:127.0.0.1:5678 USER@VPS_IP`, then open `http://localhost:5678/` on that same computer. A phone needs its own tunnel; `localhost` on the phone refers to the phone.

The installer will not mark unsecured public HTTP as ready. Public HTTPS is configured with a separate Caddy container only after the hostname's A/AAAA records pass validation and ports 80/443 are free. It validates the Caddyfile before starting the proxy and checks HTTPS with normal certificate and hostname verification. This local check does not prove reachability from every external network. If certificate issuance fails, n8n remains privately accessible and the status reports that public HTTPS is not ready. Webhooks requiring a public URL need working public HTTPS.

The n8n installation requires root, Ubuntu/Debian with systemd, at least 3 GB RAM installed, 1.2 GB RAM available, 6 GB free disk and 100,000 free inodes. Docker and Compose may already be installed. On a fresh host, installing official Docker packages requires a separate explicit choice because Docker can change firewall and network behavior. Existing containers, runtimes, reverse proxies, port listeners, and package-manager activity are inspected; conflicts stop the affected path without stopping other workloads. No host reboot, full OS upgrade, global Docker prune, or firewall reset is performed. The installer uses a pinned official n8n image, SQLite, a stable private encryption key, bounded container memory, execution-history pruning, and rotated Docker logs.

## Independent management

After n8n is installed, `sudo n8n-manage` opens its own terminal menu. CLI commands also work without the bot or Mikachan files:

```bash
sudo n8n-manage status
sudo n8n-manage diagnostics
sudo n8n-manage access
sudo n8n-manage logs
sudo n8n-manage resume
sudo n8n-manage repair
sudo n8n-manage start
sudo n8n-manage stop
sudo n8n-manage restart
sudo n8n-manage backup
sudo n8n-manage update 2.44.6
sudo n8n-manage access-set --method domain --hostname n8n.example.com
sudo n8n-manage access-set --method private
```

`resume` retries an interrupted stage using the durable journal; `repair` rebuilds container/access stages. `access-set` switches between a DNS-verified domain and private tunnel access, saves an undo copy of the previous proxy configuration, and preserves the n8n encryption key and data. Backups, the database, proxy certificates and the n8n encryption key remain in `/opt/n8n`. To restore a chosen local archive, run `sudo n8n-manage restore ARCHIVE_NAME --confirm 'RESTORE N8N'` after reviewing it. To stop/remove only the managed Compose service while keeping its data and manager, run `sudo n8n-manage remove --confirm 'REMOVE N8N SERVICE'`. Permanent n8n deletion is a separate action: `sudo n8n-manage remove --purge --confirm 'PURGE N8N DATA'`. Neither action uninstalls Docker or touches unrelated containers. An existing unowned `/opt/n8n` or n8n container is never adopted or deleted automatically.

Mikachan removal defaults to keeping n8n running, including its data, credentials, certificates, backups and manager. A noninteractive Mikachan removal never removes n8n. If the installation was interrupted, inspect `sudo n8n-manage status` and `sudo n8n-manage logs`, then use `resume`. n8n's journal is `/opt/n8n/installation.json`; secrets live in root-readable `secrets.env` and must not be pasted into support requests.

Status values distinguish `NOT_INSTALLED`, `STAGING`, `SETUP_REQUIRED`, `PRIVATE_READY`, `PUBLIC_HTTPS_READY`, `DEGRADED`, `FAILED`, and `RECOVERABLE`. `PRIVATE_READY` means the local editor responded; open an SSH tunnel from the device that will use it. `PUBLIC_HTTPS_READY` means the VPS itself verified the trusted HTTPS route. An external browser check remains advisable.
