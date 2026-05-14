# deploy/

Source-of-truth copies of every config that lives outside this repo at runtime.
Edit here, then sync into the live locations.

## systemd user units

Active copies live at `~/.config/systemd/user/`. Reapply with:

```bash
cp deploy/systemd/boat-voice.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart boat-voice
```

`tolly-sensor-sim.service` is dev-only — synthetic Signal K values via MQTT,
used to verify the sensor-query path before real SK→MQTT data flows. **Not
enabled by default.** To turn it on:

```bash
cp deploy/systemd/tolly-sensor-sim.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tolly-sensor-sim
```

To turn it off:

```bash
systemctl --user disable --now tolly-sensor-sim
```

## Home Assistant

HA runs in a Docker container with `/config` mounted from somewhere on the
host. These files are snapshots — the live copies inside the container are
the runtime truth.

- `homeassistant/configuration.snippet.yaml` — the Tolly block appended to
  `/config/configuration.yaml` (rest_command, input_boolean, rest sensor,
  template binary_sensor, lovelace dashboard registration).
- `homeassistant/dashboards/tolly.yaml` — the Tolly sidebar dashboard.
- `homeassistant/automations.yaml` — all current automations (Tolly-only at
  the moment; replace wholesale if you add unrelated automations).

To redeploy after edits:

```bash
# show what's different
diff -u <(docker exec homeassistant cat /config/dashboards/tolly.yaml) \
        deploy/homeassistant/dashboards/tolly.yaml

# push a dashboard change
docker cp deploy/homeassistant/dashboards/tolly.yaml \
          homeassistant:/config/dashboards/tolly.yaml
# HA hot-reloads YAML dashboards; refresh the browser.

# push an automations change
docker cp deploy/homeassistant/automations.yaml \
          homeassistant:/config/automations.yaml
# then in HA: Developer Tools → YAML → Reload Automations
```

The configuration.yaml snippet is more invasive — it's an append, not a
replacement, so don't blindly `docker cp`. Open `/config/configuration.yaml`
inside the container, find the `# Tolly — boat voice assistant` banner, and
replace just that block. A safe restart is `docker restart homeassistant`.
