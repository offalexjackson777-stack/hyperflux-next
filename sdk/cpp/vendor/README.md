# Vendored C++ Dependency

`include/nlohmann/json.hpp` is nlohmann/json 3.11.3, licensed under MIT.

- Upstream: <https://github.com/nlohmann/json>
- Source used for this import: the exact vendored header in OpenRGB commit
  `6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0`
- SHA-256: `9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6`
- Update policy: replace the header deliberately, update this digest, review the
  license and API delta, then run the complete C++ SDK and integration suite.

The header is vendored so packaged SDK and adapter builds remain offline and do
not depend on an untracked system installation or mutable network fetch.
