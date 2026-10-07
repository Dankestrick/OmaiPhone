# OmaiPhone

Your iPhone on the [Omarchy](https://omarchy.org) bar, over Bluetooth: take and
make calls with the PC's microphone and speakers, read and send texts, see the
iPhone's notifications, control its music and check its battery. No app on the
phone, no cloud, no Apple ID.

OmaiPhone joins two Omarchy plugins into one:
[iPhone Connect](https://github.com/Badr-Emil/omarchy-iphone-connect) by Badr
Said Emil (calls, contacts, notifications, music) and
[iMsg](https://github.com/Nikolaos-Gkionis/omarchy-imessage) by Nikolaos Gkionis
(texts). Both are MIT licensed, and so is this.

## What it does

A phone icon on the bar. Its badge counts missed calls and unread texts. Click
it for a dropdown with five tabs:

| Tab | What's there |
| --- | --- |
| Calls | Keypad, contacts (search by name or number) and recent calls. Every person has Call and Text buttons. |
| Texts | Your latest conversations. Click one to open it in the Messages card. |
| Alerts | The iPhone's notifications, with Clear and Clear all (they clear on the phone too). |
| Music | What's playing on the iPhone, with previous, play/pause and next. |
| Settings | Toggles for ringing, call pop-ups, noise suppression and pop-ups; battery and contacts; Sync contacts; Connect/Disconnect. |

When a call comes in, the PC rings and the panel opens with Answer and Decline.
During a call you get Mute, a keypad for menus, Hang up, and "Take call on this
PC" for a call you started on the iPhone.

Right-click the icon (or bind a key, below) for the **Messages card**: a
floating window in the middle of the screen for reading and sending texts,
driven by the keyboard.

## Requirements

- An iPhone and a Bluetooth adapter that holds a steady link to it. Some
  built-in Realtek combo cards drop the link (`hci0: link tx timeout` in
  `journalctl -k`); a USB adapter such as the TP-Link UB500 fixes that.
- PipeWire with WirePlumber (Omarchy's default). Calls use PipeWire's
  hands-free telephony service.
- These packages, all in Arch's `extra` repo:
  `python-gobject python-dbus-next bluez-obex pipewire-audio libpulse libnotify`

```sh
omarchy pkg add python-gobject python-dbus-next bluez-obex
```

## Install

```sh
omarchy plugin add https://github.com/dankestrick/OmaiPhone
omarchy plugin enable io.github.dankestrick.omaiphone
```

Then pair the iPhone: click the phone icon and choose **Pair iPhone**, or pair it
in Omarchy's Bluetooth panel. On the iPhone, open **Settings › Bluetooth**, tap
ⓘ next to this PC and turn on:

- **Sync Contacts**, for names on calls and texts
- **Show Notifications**, for texts
- **Share System Notifications**, for the Alerts and Music tabs

Then turn on **Show iPhone notifications** in the Alerts tab.

No service is installed and nothing runs as root. The Omarchy shell starts the
two background programs and stops them when the plugin is turned off.

## Keybindings

Each command works from a key binding (`~/.config/hypr/bindings.lua`) or a
terminal:

```sh
omarchy-shell io.github.dankestrick.omaiphone.phone panel     # open or close the panel
omarchy-shell io.github.dankestrick.omaiphone.phone messages  # open the Messages card
omarchy-shell io.github.dankestrick.omaiphone.phone answer
omarchy-shell io.github.dankestrick.omaiphone.phone hangup
omarchy-shell io.github.dankestrick.omaiphone.phone mute
```

`backend/omaiphone-ctl` is the same backend as a command line tool
(`omaiphone-ctl status`, `call <number>`, `contacts sync`, `diagnostics`).

## How it works

| Part | Bluetooth profile | Program |
| --- | --- | --- |
| Calls | HFP, through PipeWire's `org.pipewire.Telephony` | `backend/omaiphone-ctl daemon` |
| Contacts, recent calls | PBAP, through obexd | `backend/omaiphone-ctl` |
| Notifications, music | Apple's ANCS and AMS, through BlueZ's GATT client | `backend/omaiphone-ctl daemon` |
| Texts | MAP, through obexd | `backend/texts/messagesd.py` |

The phone book downloads once and both parts read it. Texts already pop up
with the sender's name, so the Messages app's own notifications don't pop up a
second time.

## Your data

Everything stays on this PC and is readable only by you:

| What | Where |
| --- | --- |
| Texts, the number you typed | `~/.local/share/omaiphone/messages.db` |
| Contacts, recent calls | `~/.local/share/omaiphone/` |
| Settings | `~/.config/omaiphone/config.json` |
| The iPhone's current notifications | `$XDG_RUNTIME_DIR/omaiphone/` (memory, gone at logout) |

Notification and message texts are never written to logs.

## Remove

```sh
omarchy plugin remove io.github.dankestrick.omaiphone
rm -rf ~/.local/share/omaiphone ~/.local/state/omaiphone ~/.config/omaiphone
```

The second line deletes your saved texts, contacts and settings.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m unittest discover -s tests/texts -t tests/texts
```

The calls tests include a simulated iPhone for the notification and music
protocol.

## License

MIT. See [LICENSE](LICENSE).
