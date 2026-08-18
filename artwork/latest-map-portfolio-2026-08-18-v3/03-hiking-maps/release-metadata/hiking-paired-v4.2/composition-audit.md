# Hiking v4 composition audit

Measured from emitted SVG paths. Main-map bounds come from each companion plot manifest and exclude the unboxed 13.8 mm bottom elevation band.

## Gates

- `detailed_min_density_mm_per_mm2`: 0.075
- `detailed_max_density_mm_per_mm2`: 0.18
- `relief_min_density_mm_per_mm2`: 0.16
- `relief_max_density_mm_per_mm2`: 0.35
- `detailed_min_occupied_fraction`: 0.2
- `relief_min_occupied_fraction`: 0.3
- `minimum_strong_family_mm`: 5
- `grid_cell_min_ink_mm`: 0.25
- `profile_band_height_mm`: 13.8
- `profile_gap_mm`: 1.8
- `flat_max_smoothed_slope_deg`: 0.75
- `flat_max_contour_span_m`: 150
- `flat_min_source_contour_levels`: 4
- `flat_min_dem_valid_fraction`: 0.95
- `flat_min_gradient_sample_count`: 1000
- `marine_min_terrestrial_valid_fraction`: 0.2
- `marine_max_terrestrial_valid_fraction`: 0.5
- `marine_min_source_contour_levels`: 8
- `marine_min_source_contour_paths`: 32
- `marine_min_contour_span_m`: 200
- `marine_min_source_relief_strokes`: 3
- `marine_min_rendered_contour_levels`: 8
- `marine_min_rendered_contour_paths`: 32
- `marine_min_rendered_terrain_mm`: 100

The density bands are the approved full-field visual grammar: West Highland Way / Great Glen Way for context and Tour des Refuges for continuous relief. Insets, profile frames and fall-line hachures are hard failures regardless of density.
A below-band relief density is accepted only when frozen source evidence proves either a genuinely flat field or a majority-marine field with substantial terrain on its terrestrial domain. Marine normalization changes only the terrain denominator used by the minimum-density gate. Occupancy, family diversity, raw maximum density and contour provenance remain hard gates.

## Artifacts

| Status | Artifact | Density raw / width-normalized | Occupancy | Contour levels | Contract |
| --- | --- | ---: | ---: | ---: | --- |
| PASS | `RTE-CA-FUNDY-01--detailed-map` | 0.130 / 0.152 | 58.9% | 6 | pass |
| PASS | `RTE-CA-FUNDY-01--terrain-relief` | 0.273 / 0.294 | 60.7% | 14 | pass |
| PASS | `RTE-CA-GDT-01--detailed-map` | 0.130 / 0.138 | 42.9% | 6 | pass |
| PASS | `RTE-CA-GDT-01--terrain-relief` | 0.221 / 0.245 | 55.4% | 9 | pass |
| PASS | `RTE-CA-WCT-01--detailed-map` | 0.133 / 0.133 | 60.7% | 5 | pass |
| PASS | `RTE-CA-WCT-01--terrain-relief` | 0.260 / 0.277 | 64.3% | 9 | pass |
| PASS | `RTE-CH-4HW-49--detailed-map` | 0.144 / 0.151 | 89.3% | 5 | pass |
| PASS | `RTE-CH-4HW-49--terrain-relief` | 0.268 / 0.296 | 91.1% | 9 | pass |
| PASS | `RTE-CH-AP6-01--detailed-map` | 0.147 / 0.147 | 82.1% | 4 | pass |
| PASS | `RTE-CH-AP6-01--terrain-relief` | 0.251 / 0.295 | 82.1% | 6 | pass |
| PASS | `RTE-CH-VA1-01--detailed-map` | 0.129 / 0.159 | 60.7% | 5 | pass |
| PASS | `RTE-CH-VA1-01--terrain-relief` | 0.202 / 0.232 | 71.4% | 7 | pass |
| PASS | `RTE-ES-CAM-ES01C--detailed-map` | 0.155 / 0.171 | 44.6% | 6 | pass |
| PASS | `RTE-ES-CAM-ES01C--terrain-relief` | 0.197 / 0.212 | 44.6% | 8 | pass |
| PASS | `RTE-ES-MAL-GR221-01--detailed-map` | 0.134 / 0.134 | 58.9% | 5 | pass |
| PASS | `RTE-ES-MAL-GR221-01--terrain-relief` | 0.196 / 0.210 | 58.9% | 8 | pass |
| PASS | `RTE-EU-POB-LOOP-01--detailed-map` | 0.140 / 0.165 | 87.5% | 5 | pass |
| PASS | `RTE-EU-POB-LOOP-01--terrain-relief` | 0.248 / 0.284 | 87.5% | 9 | pass |
| PASS | `RTE-EU-TMB-LOOP-01--detailed-map` | 0.120 / 0.120 | 89.3% | 6 | pass |
| PASS | `RTE-EU-TMB-LOOP-01--terrain-relief` | 0.247 / 0.277 | 92.9% | 12 | pass |
| PASS | `RTE-EU-WHR-01--detailed-map` | 0.141 / 0.166 | 67.9% | 5 | pass |
| PASS | `RTE-EU-WHR-01--terrain-relief` | 0.224 / 0.261 | 67.9% | 9 | pass |
| PASS | `RTE-FR-COR-GR20-01--detailed-map` | 0.142 / 0.142 | 51.8% | 6 | pass |
| PASS | `RTE-FR-COR-GR20-01--terrain-relief` | 0.220 / 0.245 | 58.9% | 8 | pass |
| PASS | `RTE-FR-ECR-976000--detailed-map` | 0.092 / 0.115 | 50.0% | 5 | pass |
| PASS | `RTE-FR-ECR-976000--terrain-relief` | 0.295 / 0.331 | 50.0% | 21 | pass |
| PASS | `RTE-FR-ECR-995181--detailed-map` | 0.134 / 0.134 | 67.9% | 4 | pass |
| PASS | `RTE-FR-ECR-995181--terrain-relief` | 0.302 / 0.334 | 100.0% | 9 | pass |
| PASS | `RTE-GB-ENG-C2C-01--detailed-map` | 0.131 / 0.163 | 57.1% | 6 | pass |
| PASS | `RTE-GB-ENG-C2C-01--terrain-relief` | 0.265 / 0.298 | 69.6% | 11 | pass |
| PASS | `RTE-GB-ENG-PW-01--detailed-map` | 0.141 / 0.158 | 46.4% | 6 | pass |
| PASS | `RTE-GB-ENG-PW-01--terrain-relief` | 0.228 / 0.244 | 48.2% | 10 | pass |
| PASS | `RTE-GB-ENG-SWCP-01--detailed-map` | 0.095 / 0.095 | 37.5% | 6 | pass |
| PASS | `RTE-GB-ENG-SWCP-01--terrain-relief` | 0.222 / 0.244 | 50.0% | 14 | pass |
| PASS | `RTE-GB-GGW-01--detailed-map` | 0.158 / 0.158 | 62.5% | 4 | pass |
| PASS | `RTE-GB-GGW-01--terrain-relief` | 0.259 / 0.271 | 83.9% | 8 | pass |
| PASS | `RTE-GB-HEB-WALK-01--detailed-map` | 0.086 / 0.090 | 51.8% | 8 | pass |
| PASS | `RTE-GB-HEB-WALK-01--terrain-relief` | 0.118 / 0.125 | 51.8% | 13 | pass |
| PASS | `RTE-GB-JMW-WALK-01--detailed-map` | 0.085 / 0.086 | 37.5% | 6 | pass |
| PASS | `RTE-GB-JMW-WALK-01--terrain-relief` | 0.294 / 0.316 | 91.1% | 12 | pass |
| PASS | `RTE-GB-SCT-CWT-01--detailed-map` | 0.084 / 0.085 | 30.4% | 6 | pass |
| PASS | `RTE-GB-SCT-CWT-01--terrain-relief` | 0.224 / 0.240 | 51.8% | 14 | pass |
| PASS | `RTE-GB-SCT-SUW-01--detailed-map` | 0.102 / 0.102 | 64.3% | 6 | pass |
| PASS | `RTE-GB-SCT-SUW-01--terrain-relief` | 0.219 / 0.248 | 82.1% | 9 | pass |
| PASS | `RTE-GB-WHW-01--detailed-map` | 0.167 / 0.167 | 67.9% | 4 | pass |
| PASS | `RTE-GB-WHW-01--terrain-relief` | 0.264 / 0.288 | 80.4% | 10 | pass |
| PASS | `RTE-GB-WLS-ODP-01--detailed-map` | 0.123 / 0.123 | 64.3% | 4 | pass |
| PASS | `RTE-GB-WLS-ODP-01--terrain-relief` | 0.214 / 0.232 | 67.9% | 6 | pass |
| PASS | `RTE-GB-WLS-PCP-01--detailed-map` | 0.134 / 0.134 | 57.1% | 6 | pass |
| PASS | `RTE-GB-WLS-PCP-01--terrain-relief` | 0.275 / 0.302 | 60.7% | 13 | pass |
| PASS | `RTE-IS-LAUG-01--detailed-map` | 0.134 / 0.134 | 89.3% | 6 | pass |
| PASS | `RTE-IS-LAUG-01--terrain-relief` | 0.276 / 0.301 | 96.4% | 12 | pass |
| PASS | `RTE-IT-DOL-AV1-01--detailed-map` | 0.085 / 0.096 | 62.5% | 4 | pass |
| PASS | `RTE-IT-DOL-AV1-01--terrain-relief` | 0.206 / 0.241 | 75.0% | 8 | pass |
| PASS | `RTE-SE-KUNGS-01--detailed-map` | 0.125 / 0.155 | 67.9% | 5 | pass |
| PASS | `RTE-SE-KUNGS-01--terrain-relief` | 0.271 / 0.303 | 76.8% | 10 | pass |
| PASS | `RTE-US-AT-01--detailed-map` | 0.127 / 0.156 | 55.4% | 6 | pass |
| PASS | `RTE-US-AT-01--terrain-relief` | 0.223 / 0.249 | 58.9% | 12 | pass |
| PASS | `RTE-US-CA-CHILKOOT-01--detailed-map` | 0.120 / 0.120 | 82.1% | 4 | pass |
| PASS | `RTE-US-CA-CHILKOOT-01--terrain-relief` | 0.267 / 0.304 | 92.9% | 8 | pass |
| PASS | `RTE-US-CDT-01--detailed-map` | 0.104 / 0.104 | 64.3% | 6 | pass |
| PASS | `RTE-US-CDT-01--terrain-relief` | 0.242 / 0.261 | 78.6% | 12 | pass |
| PASS | `RTE-US-CT-01--detailed-map` | 0.152 / 0.170 | 53.6% | 4 | pass |
| PASS | `RTE-US-CT-01--terrain-relief` | 0.261 / 0.280 | 76.8% | 7 | pass |
| PASS | `RTE-US-FNST-01--detailed-map` | 0.089 / 0.102 | 35.7% | 6 | pass |
| PASS | `RTE-US-FNST-01--terrain-relief` | 0.115 / 0.127 | 35.7% | 8 | pass |
| PASS | `RTE-US-JMT-01--detailed-map` | 0.122 / 0.122 | 55.4% | 4 | pass |
| PASS | `RTE-US-JMT-01--terrain-relief` | 0.254 / 0.271 | 55.4% | 8 | pass |
| PASS | `RTE-US-LT-01--detailed-map` | 0.103 / 0.103 | 37.5% | 4 | pass |
| PASS | `RTE-US-LT-01--terrain-relief` | 0.223 / 0.240 | 41.1% | 8 | pass |
| PASS | `RTE-US-NPT-01--detailed-map` | 0.107 / 0.107 | 62.5% | 4 | pass |
| PASS | `RTE-US-NPT-01--terrain-relief` | 0.258 / 0.299 | 89.3% | 9 | pass |
| PASS | `RTE-US-ONRT-01--detailed-map` | 0.130 / 0.149 | 33.9% | 6 | pass |
| PASS | `RTE-US-ONRT-01--terrain-relief` | 0.220 / 0.238 | 39.3% | 9 | pass |
| PASS | `RTE-US-PCT-01--detailed-map` | 0.097 / 0.118 | 35.7% | 5 | pass |
| PASS | `RTE-US-PCT-01--terrain-relief` | 0.229 / 0.250 | 42.9% | 8 | pass |
| PASS | `RTE-US-TRT-01--detailed-map` | 0.140 / 0.176 | 69.6% | 6 | pass |
| PASS | `RTE-US-TRT-01--terrain-relief` | 0.231 / 0.267 | 69.6% | 10 | pass |
| PASS | `RTE-US-WOND-01--detailed-map` | 0.151 / 0.151 | 87.5% | 6 | pass |
| PASS | `RTE-US-WOND-01--terrain-relief` | 0.276 / 0.300 | 94.6% | 13 | pass |

## Findings

- 80 artifact(s) audited; 0 failed.
