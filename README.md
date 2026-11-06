# King Smith Walking Pad

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![Community Forum][forum-shield]][forum]

[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]


Custom integration to connect Home Assistant with [King Smith Walking Pads][king_smith].

This integration adds a belt switch, a speed control, walking sensors, and a persistent session recorder. Speed and distance use metric native units and Home Assistant can display them in your preferred units.

This integration discovers/connects to the device using bluetooth (you may need to configure a bluetooth proxy like an ESP32).

It has been tested with a C2 (the model I personally own), but is likely to work with other models supported by the underlying library.

In order for the device to be connected to HA, it must not be connected to another device (e.g. the mobile app). If it is already configured via the mobile app, you will have to disconnect it before using it in HA.

## Installation

1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
1. If you do not have a `custom_components` directory (folder) there, you need to create it.
1. In the `custom_components` directory (folder) create a new folder called `king_smith`.
1. Download _all_ the files from the `custom_components/king_smith/` directory (folder) in this repository.
1. Place the files you downloaded in the new directory (folder) you created.
1. Restart Home Assistant
1. Click on `Settings` and then `Devices & services`
1. If the treadmill is on and not currently connected to another device, it should show up in the `Discovered` list. Click `configure` and give it a name.

You can also add it manually: go to **Settings > Devices & services > Add integration**, search for **King Smith Walking Pad**, and enter a name and the treadmill's Bluetooth MAC address (for example, `AA:BB:CC:DD:EE:FF`). The treadmill must be powered on and reachable through Home Assistant's Bluetooth adapter or a Bluetooth proxy.

If you see `not_implemented` when adding the integration, update the files in `custom_components/king_smith/` (including the `translations` directory) from this repository and restart Home Assistant before trying again.

## Configuration is done in the UI

## Development releases in HACS

Development builds of this fork are published as GitHub prereleases from the `dev` branch. HACS lists published releases, not arbitrary branches.

1. Add `https://github.com/s-bed/king_smith` as a custom repository in HACS with the **Integration** category if it is not already added. Make sure you are downloading this fork rather than `indiefan/king_smith`.
1. Open **King Smith** in HACS, open its menu, and refresh its information.
1. Choose **Redownload**, enable **Show beta versions** (or the repository's prerelease option), and select **0.1.3-dev.3**.
1. Download it and restart Home Assistant.
1. Go to **Settings > Devices & services > Add integration > King Smith Walking Pad** to configure it.

When publishing another development build, use a new prerelease version in `manifest.json` and publish a GitHub prerelease with that same tag from the development branch. A branch or tag alone does not make a selectable HACS release.

The `0.1.3-dev.2` build includes the [six changes from Hypfer's fork](https://github.com/Hypfer/king_smith/compare/7e01359fdaed684dbc768678a591c845b67cf61d...a299c3ccbb76c814a335f29140b59dc32188d124): cadence averaged over the last ten readings, R1 Pro Bluetooth discovery, `ph4-walkingpad>=1.0.2`, one shared command lock, polling every five seconds through the coordinator, and quieter offline/disconnection handling. It also includes the sensor docstring fixes and explicit HACS branch/download settings from those commits. Manual setup remains available.

## Session recording and Garmin review

Starting with `0.1.3-dev.3`, observed belt movement records sessions automatically. Stops shorter than two minutes are merged into the same activity, with paused time excluded from moving time. A longer stop saves the activity using the observed stop time. If Bluetooth data disappears for two minutes, the recorder saves an interrupted activity through the last received sample. A session first observed while already moving is marked partial because its earlier data was not recorded.

Sessions include timestamps, moving/elapsed duration, distance, steps, speed and cadence samples. The integration saves JSON and indoor-walking FIT files in `walkingpad_sessions/<entry_id>/` inside your Home Assistant configuration directory. Active recording is checkpointed regularly and recovered after restarts. Files stay outside the public `www` directory. Your normal configuration backups may contain these fitness recordings.

The **Recorder state** and **Pending sessions** sensors show recording and review progress. Saved sessions can be inspected using **Developer tools > Actions > King Smith: List saved WalkingPad sessions**. Supply the WalkingPad device; the response includes each session UUID, summary and status. The upload/discard actions use that UUID rather than accepting an arbitrary file path.

### Install the phone review blueprint

HACS installs the integration files. Import this blueprint separately after updating and restarting Home Assistant:

[![Import WalkingPad review blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fs-bed%2Fking_smith%2Fblob%2F0.1.3-dev.3%2Fblueprints%2Fautomation%2Fking_smith%2Freview_walkingpad_session.yaml)

[Open the blueprint source](https://github.com/s-bed/king_smith/blob/0.1.3-dev.3/blueprints/automation/king_smith/review_walkingpad_session.yaml)

1. Ensure the Home Assistant Companion App is registered and allowed to receive notifications on the phone you want to use.
1. Install/configure Garmin Connect with activity upload support. This feature targets the installed Garmin Connect `3.0.21` actions.
1. Import the blueprint and create one automation per WalkingPad.
1. Select the WalkingPad, notification phone, and a Garmin Connect sensor belonging to the destination account.
1. Leave the minimum moving duration at **60 seconds**, or change it. Shorter walks are still saved locally.

After a qualifying session is saved, the selected phone receives its moving duration, distance and steps. Interrupted recordings are labelled incomplete. Choose **Upload** to approve its FIT upload to the selected Garmin account, or **Discard** to skip Garmin while keeping the local recording. Review notifications are restored after a Home Assistant restart; they use saved session IDs, so old buttons do not create fresh uploads.

King Smith reuses the existing Garmin Connect login through its public upload action and stores no additional Garmin credentials. Missing Garmin services or an unloaded account leave an approved upload waiting. If an upload outcome is uncertain, the notification asks you to check Garmin before choosing **Checked Garmin: retry**. Repeated button taps are handled idempotently. The Garmin action confirms acceptance but does not return an activity ID, so uncertain uploads are never automatically resent.

Uploaded and discarded recordings are retained for **90 days after resolution**, then cleaned up locally. Unresolved sessions and upload failures are retained until resolved. **Discard** does not delete a Garmin activity or cancel an upload already in progress. It only marks the local session as excluded from upload.

The FIT file contains the recorded indoor walking data, including separate moving/elapsed time and pauses. No GPS route, heart rate or calories are invented. Bluetooth sampling is normally around five seconds, so very short transitions and missing packets limit timing precision. Decoder and automated tests verify the file structure; physical treadmill recording and Garmin's displayed interpretation still need live validation after installation.

## Verified Supported Models

* C2
* A1 Pro

(if you have a King Smith treadmill and would like to verify support, please reach out)

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

***

[king_smith]: https://github.com/ludeeus/integration_blueprint
[buymecoffee]: https://www.buymeacoffee.com/indiefan
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[commits-shield]: https://img.shields.io/github/commit-activity/y/indiefan/king_smith.svg?style=for-the-badge
[commits]: https://github.com/indiefan/king_smith/commits/main
[exampleimg]: example.png
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/
[license-shield]: https://img.shields.io/github/license/indiefan/king_smith.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/indiefan/king_smith.svg?style=for-the-badge
[releases]: https://github.com/indiefan/king_smith/releases
