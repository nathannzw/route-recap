# route-recap 🗺️

Inspired by a recent road trip: after coming back with heaps of geotagged photos and videos, existing tools didn't offer a clean, fully controlled, native way to view the whole journey. `route-recap` is a custom solution built to turn raw travel media into an automated, continuous route map summary.

### 🎯 Core Feature
* **Media-to-Route Pipeline:** Extracts EXIF geotags and timestamps, sorts waypoints chronologically, and calls routing APIs to map out the exact driving path.

### 🔮 Future Roadmap
* **Stay & Stop Detection:** Identify lodging and rest stops using spatial/temporal clustering.
* **LLM Layer:** Parse key highlight photos and generate trip journals / leg summaries.
* **Native Interface:** Custom UI to explore interactive maps synced with full-res media.
