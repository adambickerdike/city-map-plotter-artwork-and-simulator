# Third-party notices

## Hershey Serif Medium stroke-font data

The packaged `hershey-serif-medium.json` display face is converted from
`HersheySerifMed.svg`, originally prepared in 2011 and converted to SVG fonts
in 2019 by Windell H. Oskay of Evil Mad Scientist.

The Hershey Fonts may be used for any purpose, commercial or otherwise,
provided these acknowledgements accompany the font data:

- The Hershey Fonts were originally created by Dr. A. V. Hershey while working
  at the U. S. National Bureau of Standards.
- The format of the font data in this distribution was originally created by
  James Hurt, Cognition, Inc., 900 Technology Park Drive, Billerica, MA 01821.

The font data must not be converted into the U.S. NTIS eight-byte
`"xxx yyy:"` point format. This project stores ordinary JSON polylines instead.

## Optional serial controller transport

Live GRBL streaming uses the optional `pyserial==3.5` package. PySerial is
BSD-3-Clause software, copyright © 2001–2020 Chris Liechti. Source and binary
redistribution must retain its copyright, conditions, and disclaimer, and may
not use the copyright holder's or contributors' names for endorsement without
permission:

- <https://pypi.org/project/pyserial/3.5/>
- <https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt>

## OpenStreetMap hiking geography

Hiking records that declare an OpenStreetMap source contain independently
styled route and/or contextual geography derived from identified OpenStreetMap
features. OpenStreetMap data is licensed under the Open Database License (ODbL)
by the OpenStreetMap Foundation. Those rendered plates visibly print the
historically accepted compact credit `© OpenStreetMap` together with the full
copyright URL, and their manifests retain the applicable relation or element
identity, source URL, snapshot evidence, use, geometry status, and ODbL
identifier:

- © OpenStreetMap contributors
- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

The plates are decorative produced works, are not official route maps, and must
not be used for navigation.

### Waymarked Trails and Overpass acquisition services

The expanded hiking catalogue uses the Waymarked Trails API to identify and
retrieve details for named OpenStreetMap route relations. It does not copy
Waymarked Trails map tiles, cartographic styling, icons, or page artwork. The
rendered route coordinates remain attributed to OpenStreetMap contributors
under the ODbL:

- <https://hiking.waymarkedtrails.org/>
- <https://www.openstreetmap.org/copyright>

Selected OpenStreetMap roads, hydrography, land cover and geographic labels are
queried through public Overpass API instances. An Overpass endpoint transports
the query result; it is not a substitute licensor for the underlying
OpenStreetMap data. Public endpoints also are not permanent source archives, so
the release records exact query and response hashes and must respect the
operator's current usage policy:

- <https://wiki.openstreetmap.org/wiki/Overpass_API>

### AWS-hosted Mapzen terrain tiles

The paired global-relief examples derive route/profile elevations and selected
continuous contours from Mapzen Terrarium tiles hosted through the AWS Open
Data programme. Frozen source bundles may retain DEM-gradient fall-line
evidence for provenance, but neither v4.2 artwork variant emits those scratch
hachures:

- <https://registry.opendata.aws/terrain-tiles/>
- <https://github.com/tilezen/joerd/blob/master/docs/attribution.md>

This is a composite terrain product. The source records deliberately state
`mixed source terms; location-specific review required` and flag underlying
provider attribution for review. AWS hosting does not provide blanket
commercial clearance and does not replace the terms or credit required by the
terrain provider covering a particular route. The generated examples therefore
remain review-only until those location-specific obligations have been resolved
and the required source-specific credit has been placed on the product.

## OpenStreetMap architecture studies

The standalone stadium, landmark, and building pilot plates contain
independently styled plan geometry and explicit tag-derived height evidence from
pinned OpenStreetMap snapshots. Their visible furniture credits OpenStreetMap
contributors, while each manifest retains the exact element URL, snapshot path
and SHA-256, canonical model SHA-256, use, evidence status, and ODbL identifier:

- © OpenStreetMap contributors
- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

These plates are source-derived architectural studies, not surveys,
construction drawings, as-built records, or endorsements by the depicted
venues or institutions. The separate concept-house record is project-authored
and contains no geographic or private-address source geometry.

## Geographic rail and urban-transit studies

The transit subsystem creates original geographic service overlays. It does
not copy operator diagrams, logos, roundels, branded typefaces, schematic
geometry, or trade dress. London Underground, Tyne and Wear Metro, Glasgow
Subway, and Sheffield Supertram use identified OpenStreetMap route relations
for geographic ordering, with dated operator material used only to check
service scope and colour facts. Those operator references remain
permission/review-gated for commercial production.

OpenStreetMap-derived route and context data remain subject to ODbL 1.0 and
the visible attribution requirements recorded in each plot manifest:

- © OpenStreetMap contributors
- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

Manchester Metrolink uses Transport for Greater Manchester's functional
map-data archive under the Open Government Licence and its dated GTFS feed
under ODbL 1.0. The source contract and visible artwork retain TfGM, Ordnance
Survey, public-sector-information, OpenStreetMap, OGL, and ODbL notices. The
two TfGM operator maps are human references only and remain
permission-required; neither is traced. ODbL Produced Work and any conditional
Derivative Database/distribution duties are a release-review gate, not implied
commercial clearance.

- <https://odata.tfgm.com/opendata/downloads/MetrolinkMapData/GM_Metrolink_MapData.zip>
- <https://odata.tfgm.com/opendata/downloads/TfGMgtfsnew.zip>
- <https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/>
- <https://opendatacommons.org/licenses/odbl/1-0/>

LNER, Great Western Railway, Southern, and Northern are catalogued but remain
source-gated until the dedicated licensed timetable, station-identity, and
alignment compiler exists. Their published maps are factual scope references,
not geometry assets.

### OS Open Zoomstack physical-rail context

The optional national physical-rail context reads an exact, locally pinned
vector-tile snapshot of OS Open Zoomstack. The project uses only independently
styled rail linework; it does not copy Ordnance Survey styles, symbols, or
artwork. OS Open Zoomstack is OS OpenData supplied under the Open Government
Licence 3.0. Reproductions and derived material must retain the acknowledgement
applicable to the June 2026 product snapshot:

- Contains OS data © Crown copyright and database right 2026.
- <https://osdatahub.os.uk/downloads/open/OpenZoomstack>
- <https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/>

The optional vector-tile decoder and its pinned transitive dependencies are:

- `mapbox-vector-tile==2.2.0`, MIT; copyright © 2014 Mapzen.
- `protobuf==6.33.6`, BSD 3-Clause; copyright 2008 Google Inc.
- `pyclipper==1.4.0`, MIT; copyright © 2015 Gregor Ratajc, Lukas
  Treyer, and Maxime Chalton.
- Clipper 6.4.2 code included by Pyclipper, Boost Software License 1.0;
  copyright © Angus Johnson 2010–2017.

The exact upstream licence files are available at:

- <https://github.com/tilezen/mapbox-vector-tile/blob/v2.2.0/LICENSE>
- <https://github.com/protocolbuffers/protobuf/blob/v33.6/LICENSE>
- <https://github.com/fonttools/pyclipper/blob/1.4.0/LICENSE>
- <https://www.boost.org/LICENSE_1_0.txt>

The MIT-licensed components are provided under these terms (with the respective
copyright notices above):

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

The Protobuf runtime is provided under these BSD 3-Clause terms:

> Copyright 2008 Google Inc. All rights reserved.
>
> Redistribution and use in source and binary forms, with or without
> modification, are permitted provided that the following conditions are met:
>
> - Redistributions of source code must retain the above copyright notice,
>   this list of conditions and the following disclaimer.
> - Redistributions in binary form must reproduce the above copyright notice,
>   this list of conditions and the following disclaimer in the documentation
>   and/or other materials provided with the distribution.
> - Neither the name of Google Inc. nor the names of its contributors may be
>   used to endorse or promote products derived from this software without
>   specific prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
> AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
> IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
> ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
> LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
> CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
> SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
> INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
> CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
> ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
> POSSIBILITY OF SUCH DAMAGE.
>
> Code generated by the Protocol Buffer compiler is owned by the owner of the
> input file used when generating it. This code is not standalone and requires
> a support library to be linked with it. This support library is itself covered
> by the above license.

Clipper 6.4.2 is provided under the Boost Software License 1.0:

> Permission is hereby granted, free of charge, to any person or organization
> obtaining a copy of the software and accompanying documentation covered by
> this license (the "Software") to use, reproduce, display, distribute,
> execute, and transmit the Software, and to prepare derivative works of the
> Software, and to permit third-parties to whom the Software is furnished to do
> so, all subject to the following:
>
> The copyright notices in the Software and this entire statement, including
> the above license grant, this restriction and the following disclaimer, must
> be included in all copies of the Software, in whole or in part, and all
> derivative works of the Software, unless such copies or derivative works are
> solely in the form of machine-executable object code generated by a source
> language processor.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE, TITLE AND NON-INFRINGEMENT. IN NO EVENT
> SHALL THE COPYRIGHT HOLDERS OR ANYONE DISTRIBUTING THE SOFTWARE BE LIABLE FOR
> ANY DAMAGES OR OTHER LIABILITY, WHETHER IN CONTRACT, TORT OR OTHERWISE,
> ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
> DEALINGS IN THE SOFTWARE.

## Frozen university map source responses

The ranked university reproducibility contract includes 50 subject-keyed,
compressed Overpass responses captured on 3 August 2026. These are frozen map
data inputs rather than project-authored code. Their manifest records the exact
compressed response hash, canonical decoded JSON hash, query hash, OSM base
timestamp, and reviewed extent for every subject:

- © OpenStreetMap contributors
- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

The snapshots permit offline repetition of the reviewed university geometry.
They remain subject to the ODbL and do not by themselves satisfy the physical
pen-calibration or production-release gates.

## OpenStreetMap golf-course studies

The Twenty-Five Icons of Golf collection contains independently styled course
geometry from 25 pinned OpenStreetMap API extracts. Every emitted course
feature retains its OSM object reference, version and timestamp; the catalog
and source contract retain deterministic extract hashes. OpenStreetMap data is
licensed under the Open Database License (ODbL):

- © OpenStreetMap contributors
- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

Official club, R&A, tournament, and venue pages are used as factual references
for names and concise championship/geographic context only. Their course maps,
scorecard graphics, logos, photography, and trade dress are not traced. The
plates are source-derived studies, not surveys, navigation aids, official
scorecards, or endorsements by the identified clubs or championships.

## Formula 1 circuit-atlas source studies

The F1 circuit-atlas catalog contains independently styled circuit and context
geometry from frozen, versioned OpenStreetMap objects. Every emitted sourced
feature binds its source reference and stable object identity; the catalog,
SVG, manifest, and release index bind the same canonical geometry digest.
OpenStreetMap data is licensed under the Open Database License (ODbL):

- © OpenStreetMap contributors
- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

FIA and Formula 1 pages are used only to establish current calendar status and
concise circuit facts. Their maps, circuit illustrations, photography, logos,
broadcast graphics and trade dress are not traced. Open map-data licensing does
not grant circuit-outline, event-name, venue, promoter, sponsorship or
merchandising rights. The generated plates are review-only independent studies
and do not imply Formula 1, FIA, promoter, team or venue endorsement.

## Nürburgring and Le Mans endurance-circuit studies

The motorsport circuit-study source pack contains independently styled full
course centrelines assembled from exact frozen OpenStreetMap objects. The
Nürburgring study joins the mapped Grand Prix course and Nordschleife through
named 24-hour connection ways; the Le Mans study uses the full Circuit de la
Sarthe relation. The source manifest retains every payload and compressed-file
SHA-256. OpenStreetMap data is licensed under the Open Database License (ODbL):

- © OpenStreetMap contributors
- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

The ADAC RAVENOL 24h Nürburgring and Automobile Club de l'Ouest pages are
used only as factual references for the published configuration descriptions
and lengths. Their maps, illustrations, logos, photography and event trade
dress are not traced. Le Mans is catalogued as a general motorsport study and
is not presented as a Formula 1 course. Both plates remain review-only and do
not imply organiser, venue or championship endorsement.

## Parc national des Écrins open hiking data

The two Écrins hiking examples contain selected route facts from the French
public dataset “Randonnées du Parc national des Écrins”; the historical
ten-route edition also used its elevation facts. The source is attributed on the
artwork and recorded in the manifest as Licence Ouverte / Open Licence 2.0 data:

- <https://www.data.gouv.fr/datasets/randonnees-du-parc-national-des-ecrins>
- <https://www.etalab.gouv.fr/licence-ouverte-open-licence/>

## Camino de Santiago open route data

The Camino Francés example contains selected route facts from the CNIG/FEAACS
Camino de Santiago download; the historical ten-route edition also used its
elevation facts. The source is identified in the manifest and credited on the
artwork. The catalogue records the supplied geometry as CC BY 4.0:

- <https://centrodedescargas.cnig.es/CentroDescargas/camino-santiago>
- <https://creativecommons.org/licenses/by/4.0/>
