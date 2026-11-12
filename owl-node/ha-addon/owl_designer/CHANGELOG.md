# Changelog

## 0.7.2
- Documentation tab rewritten, with a picture and first steps.
- Designer: Panels pages fit phone screens (a row of page tabs made them wider than the screen); auto-placed blocks no
  longer land on top of each other; long block names are cut off instead of running over their badges.

## 0.7.1
- First add-on release: the designer and Routing page in the Home Assistant sidebar (Ingress), with the audio engine on
  another machine (`engine_url`). Optional LAN port 8361.
- Ships as a prebuilt image for amd64 and aarch64 (`ghcr.io/morningstarowl/owl-designer-addon:0.7.1`): Home Assistant
  pulls it and builds nothing.
