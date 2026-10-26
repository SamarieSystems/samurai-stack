# The user guide

This is the day-to-day manual for a Samurai Stack media server. You do not need to learn Linux or Docker to request a film, watch a show or check a download. Server maintenance has its own chapters when you need them.

If somebody already installed the server for you, ask them for the private service links, your everyday viewer login and the completed [handover checklist](HANDOVER-CHECKLIST.md). The public manual cannot know your server's address or password.

## Pick what you want to do

| I want to... | Open this chapter |
| --- | --- |
| Understand what I have and where to start | [Start here](START-HERE.md) |
| Watch something, request something or use subtitles | [Daily use](DAILY-USE.md) |
| Find a download, understand imports or add existing media | [Media and downloads](MEDIA-AND-DOWNLOADS.md) |
| Install on a new Linux VM, with each prompt explained | [New-server walkthrough](INSTALL-A-NEW-SERVER.md) |
| Use Dockge, check space or understand startup | [Operating the server](OPERATING-THE-SERVER.md) |
| Fix a symptom without guessing | [Troubleshooting](TROUBLESHOOTING.md) |
| Keep the server recoverable or update it | [Backups and updates](BACKUPS-AND-UPDATES.md) |
| Hand this server to its owner | [Handover checklist](HANDOVER-CHECKLIST.md) |
| Decode a word such as container, hardlink or indexer | [Glossary](GLOSSARY.md) |

## Three account types

- Your **everyday viewer** account is for watching media and making the requests your administrator permits.
- Your **application administrator** login changes the media services. Different apps may have different administrator credentials.
- Your **Linux or Proxmox** login manages the machine. It is separate from the accounts above and is not needed for ordinary watching.

Use the viewer account for normal use. Your installer or server owner should keep the administrative and provider credentials privately, outside this public repository.

## Reading offline or printing

The documentation pack contains `index.html`. Extract the ZIP and open that file in a normal browser. It includes this guide and the technical reference, with a search box and working chapter links. No account, package installation or website hosting is required. Online provider/source links are marked `[web]` and still need internet.

Choose **Print user guide / PDF** to print the everyday chapters or save them as a PDF. Normal browser printing uses that same scope. Choose **Print complete manual** only when you also want the detailed technical reference. Printing includes every chapter in the chosen scope, even if a search currently hides some on screen. The pack is documentation, not an installer.

## Installing a new server

Start with the [new-server walkthrough](INSTALL-A-NEW-SERVER.md); the
[guided quickstart](../QUICKSTART.md) gives the detailed setup reference.
New server installation, connecting an existing disk and changing Proxmox
hardware are separate from using an already installed server. Follow the
[deployment intake](../DEPLOYMENT-NIGHT.md) before choosing storage or replacing
an operating system.

## Exact settings and advanced work

The public guide explains normal tasks. The [technical service reference](../SERVICES.md), [storage guide](../STORAGE.md), [recovery procedures](../RECOVERY.md) and [validation record](../VALIDATION.md) give the detailed mechanics and measured limits. A feature appearing in a manual does not prove it has been enabled or tested on your particular server.

To build a fresh offline pack from a source checkout, use Python 3.11 or later:

```sh
python3 scripts/build-user-guide.py --output /tmp/samurai-user-guide
```

Run this from the repository root. Choose a new output directory outside the checkout; the builder refuses to replace an existing directory. It reads documentation only and does not contact or change your server. The output contains a standalone HTML file, ZIP, documentation manifest and `USER-GUIDE-SHA256SUMS`.
