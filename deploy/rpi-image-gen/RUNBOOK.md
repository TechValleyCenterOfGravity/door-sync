# door-sync appliance runbook

Operating a flashed door-sync appliance: first boot, changing configuration,
checking health, and updating. For how the image is *built*, see `README.md`.

**Nothing here has been exercised on a real device yet.** The image builds in
CI but has not been flashed. Treat every procedure as needing confirmation on
first use, and correct this file when it disagrees with reality.

## The one thing to know first

A freshly flashed card is **loudly broken until it is provisioned**, by design.
`door-sync.service` has `EnvironmentFile=/etc/door-sync/env` with no leading
`-`, so a missing secrets file is a hard start failure. With
`Restart=on-failure` and `RestartSec=30s`, the unit retries every 30 seconds
indefinitely and the journal fills with the same error.

That is fail-secure and expected. It is not a broken image.

## First boot

Step 1 needs a console on the device — a keyboard and monitor, or a serial
console. There is no remote shell before sign-in, because sign-in is what
creates it. Steps 2 onward can all be done over the Connect remote shell,
because Connect sign-in is independent of door-sync's config: the device is
reachable while door-sync is still restart-looping.

Screen sharing is *not* available: it requires Wayland and does not work on
Raspberry Pi OS Lite, which this image is built from.

1. **Sign the device in to Connect.** From the console:

   ```sh
   sudo rpi-connect on
   rpi-connect signin
   ```

   `signin` prints a verification URL of the form
   `https://connect.raspberrypi.com/verify/XXXX-XXXX`. Open it on any device,
   sign in with your Raspberry Pi ID, and the link completes — nothing has to
   run a browser on the Pi, which is what makes this work on Lite. Confirm with
   `rpi-connect status`.

   Then opt the device in to remote updates, which is a separate switch:

   ```sh
   rpi-connect ota on
   ```

   Connect signs communication with the device's serial number, so moving this
   card to a different Pi signs it out and you repeat this step.

2. **Write the config.** Start from the shipped example rather than from
   memory; it documents every key and its default:

   ```sh
   sudo cp /usr/share/door-sync/config.example.toml /etc/door-sync/config.toml
   sudo nano /etc/door-sync/config.toml
   ```

3. **Write the secrets.** Always needed: `CIVICRM_API_KEY` and `UNIFI_API_KEY`.
   `WEBHOOK_HMAC_SECRET` is needed only when `webhook.enabled` is true, and
   then it is required. `SMTP_USERNAME` + `SMTP_PASSWORD`, or
   `MAILGUN_API_KEY`, are needed only for the matching alert transport;
   the default flag-file transport needs neither.

   ```sh
   sudo cp /usr/share/door-sync/env.example /etc/door-sync/env
   sudo nano /etc/door-sync/env
   sudo chown door-sync:door-sync /etc/door-sync/env
   sudo chmod 0400 /etc/door-sync/env
   ```

   **Both the `chown` and the `chmod` matter, in that order.** The `chmod` is
   what stops the API keys being world-readable — a file created by the usual
   umask is 0644. The `chown` is what keeps door-sync able to read them:
   systemd reads `EnvironmentFile=` as root, but door-sync *also* reads the
   file itself, as `User=door-sync`. Leave it root-owned at 0400 and the
   service fails while `sudo validate-config` still passes, because root
   ignores the mode.

   Prefer editing in place over pasting a heredoc: a heredoc body becomes part
   of the command line, so `sudo tee ... <<EOF` writes your API keys into shell
   history.

4. **Check it before starting anything:**

   ```sh
   sudo door-sync --config /etc/door-sync/config.toml \
                  --env-file /etc/door-sync/env validate-config
   ```

   The paths are not optional. `door-sync.service` sets
   `DOOR_SYNC_CONFIG_DIR=/etc/door-sync` for the *service*; an interactive
   shell has no such variable, so a bare `door-sync validate-config` looks for
   `./config.toml` in whatever directory you happen to be in. If you would
   rather not type them, `sudo env DOOR_SYNC_CONFIG_DIR=/etc/door-sync
   door-sync validate-config` is equivalent.

   This reports every problem at once, not just the first, and fails on a
   permissive env file as well as on invalid values.

5. **Start it:**

   ```sh
   sudo systemctl restart door-sync
   sudo systemctl status door-sync
   ```

6. **Watch one cycle go through** before walking away:

   ```sh
   sudo journalctl -u door-sync -f
   ```

## Changing configuration

`/etc/door-sync` is a slot-shared writable bind mount, so it is editable in
place on the read-only root and survives an A/B slot flip. Edit, validate,
restart — and keep the `&&`, so an invalid config never reaches a restart:

```sh
sudo nano /etc/door-sync/config.toml
sudo door-sync --config /etc/door-sync/config.toml \
               --env-file /etc/door-sync/env validate-config \
  && sudo systemctl restart door-sync
```

If you restart with a bad config anyway, the service will not start and the
journal names every problem:

```sh
sudo journalctl -u door-sync -n 50
```

Config is deliberately **not** managed by the image. Baked config would be
pushed whenever it differs, silently reverting on-device edits to the safety
thresholds an operator may need to tune. See `README.md`.

## Checking health without reading logs

| What | Where | Means |
| --- | --- | --- |
| Alert flag | `/var/lib/door-sync/alert.flag` | Present = the last cycle halted. Cleared only by a successful cycle. |
| State | `/var/lib/door-sync/state.json` | Last success and last halt timestamps. |
| Audit log | `/var/log/door-sync/audit.jsonl` | One record per cycle outcome, append-only. |

All three are slot-shared, so they survive an OTA. The alert flag is latched
deliberately: it used to live in `/run` and vanished on every reboot, including
the reboot a slot flip performs.

Members appear in logs as `contact_id`, never by name, and card IDs are
redacted to the last four digits. That is a hard rule, not an accident of
formatting — do not "improve" it.

## Updates

Updates are A/B OTA bundles pushed through Raspberry Pi Connect: the device
writes the inactive slot and flips, reverting automatically if the update
fails. Building and deploying a bundle is covered in `README.md` and in
Raspberry Pi's Remote Update documentation.

After an update, confirm the service came back:

```sh
sudo systemctl status door-sync
cat /var/lib/door-sync/state.json
```

A slot flip does not touch `/etc/door-sync`, so configuration carries over
untouched. New releases must not *require* a new config key for exactly this
reason — see the hard rule in `CLAUDE.md`.

## If the tunnel is down

The webhook is reached through a Cloudflare Tunnel. If deliveries stop
arriving, door-sync's own logs will say nothing at all, because the request
never reaches it. Check the tunnel first:

```sh
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 50
```

Tunnel credentials live in `/etc/cloudflared`, which is slot-shared for this
reason — an OTA that lost them would silently stop the webhook.

## Clock

The Pi has no RTC. `door-sync.service` orders itself after `time-sync.target`
because the webhook rejects signatures outside `max_skew_seconds` (default 300).
If webhook deliveries fail with signature errors right after a power cut,
check the clock before suspecting the secret:

```sh
timedatectl
```
