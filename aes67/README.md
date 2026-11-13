# aes67/

The linuxptp configurations `owl-node/install.sh` installs for the AES67 media clock (L2 transport, hardware
timestamps, domain 0, 1/8 s sync):

| File | Installed by | Role |
|---|---|---|
| `ptp4l-grandmaster.cfg` | `--ptp grandmaster` (default on a hub) | the house's grandmaster (priority1 100) |
| `ptp4l-backup.cfg` | `--ptp backup` (default on a node with a PTP hardware clock) | follows the hub; if the hub goes away, the backups elect one of themselves as grandmaster (priority1 200) so the nodes stay aligned; the hub takes over again when it returns |
| `ptp4l-follower.cfg` | `--ptp follower` | follows only (slaveOnly): never leads, so with the hub down it free-runs |

A node installed before `backup` existed keeps its follower config (`--ptp keep` is the default when
`owl-ptp4l.service` exists); re-run `owl-node/install.sh --ptp backup` on it to opt in.
