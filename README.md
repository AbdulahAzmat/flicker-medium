# Flick

Throw photos, PDFs and documents from your phone to this laptop with a flick.

## Use it

Flick starts by itself when you sign in to Windows and waits in the system tray (the arrow icon
near the clock, possibly under the `^`). You don't need to open anything on the laptop.

On your phone:

1. Open Flick (add the page to your home screen once, then it's one tap).
2. Tap **Photos**, **Camera**, **Files** or **Paste**.
3. **Swipe the card up**, or turn on **Phone flick** and flick the phone toward the laptop.
   The chip cycles normal → firm → soft → off to set how hard a flick has to be.

The file lands in `Downloads\Flick`, and a small card drops in from the top of the screen showing
what arrived and from which phone, while a soft light rings the edges of the screen in a
tick-tock double pulse. It floats over whatever you're working in, never takes keyboard focus,
and leaves after about three and a half seconds. Click it to open the file (or the folder, for several).

Whatever lands is also copied to the clipboard, so you can press Ctrl+V straight away:

- into a folder, chat, email or upload box, it pastes the file (or all the files from one flick);
- into Paint, Photoshop or a document, a single photo pastes as a picture;
- into any text box, a single small text note (such as one sent with **Paste** on the phone) pastes as text.

This replaces whatever was on the clipboard. Set `"copy_to_clipboard": false` in `config.json` to turn it off.

The light and the card take the colour of what arrived:

| Arrived | Colour |
| --- | --- |
| Photos and other images | Light purple |
| PDFs | Light red |
| Documents, slides, spreadsheets, text | White |
| Videos | Light blue |
| Audio | Light amber |
| Anything else, or a mix | Mint |

The colours are the `COLOURS` table at the top of `overlay.py`.

Set `"landing_card": false` in `config.json` to get a plain Windows notification instead.
`python overlay.py pdf` shows a demo card (also `image`, `doc`, `video`, `audio`, `file`).

### First time on a new phone

1. Right-click the tray icon → **Pair a phone**, and scan the QR code. Same Wi-Fi as the laptop.
2. The phone warns the certificate isn't trusted, because the laptop made it itself. Continue anyway
   (Chrome: Advanced → Proceed. Safari: Show Details → visit this website).
3. Tap **rename** under the buttons to give the phone a name. The laptop shows it next to
   everything that phone sends, and in the list of phones connected right now.

## The tray icon

| Menu item | What it does |
| --- | --- |
| Open Flick (or double-click) | The laptop page: connected phones, received files, landing animation |
| Open received files | Opens the inbox folder |
| Pair a phone | Shows the QR code |
| Start when I sign in | Tick or untick |
| Quit Flick | Stops receiving until you start it again with `Start Flick.bat` |

## How it stays reliable

- Files upload as soon as you pick them and are held back until you flick, so the throw is instant.
- A dropped connection retries by itself. If the laptop really can't be reached, the card comes
  back and the next flick tries again. Nothing is lost.
- The dot at the top of the phone page is live: green means the laptop is reachable right now.
- If your Wi-Fi gives the laptop a new address, Flick switches to it and tells you to scan the QR
  code again. Reserving a fixed address for the laptop in your router settings avoids this.
- Files you picked but never flicked are deleted from the laptop after an hour, or when you tap **Clear**.

## Settings

`config.json` is created on first run:

| Key | Meaning |
| --- | --- |
| `folder` | Where files land. Default `Downloads\Flick`. |
| `token` | Pairing secret baked into the QR code. Delete the line to un-pair every phone. |
| `phone_port`, `desk_port` | Ports for the phone (HTTPS, whole network) and the laptop page (this machine only). |

Quit Flick from the tray and start it again after editing.

## Notes

- Needs Python with `aiohttp`, `cryptography`, `qrcode`, `pystray` and `Pillow`.
- `python server.py` runs it in a console window instead, with the QR code printed there.
- Only a phone that scanned the QR code can send files, and only the laptop itself can open the laptop page.
- Problems are written to `flick.log` in this folder.
- `http://127.0.0.1:7080/?phone` previews the phone page on the laptop.
