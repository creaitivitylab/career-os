# Offline geography data

`iso-geography.json` is a deterministic subset (country and subdivision identity/name/type fields) of the installed Debian `iso-codes` 4.16.0-1 JSON files, extracted on 2026-10-06. It contains 249 ISO 3166-1 countries and 5,046 ISO 3166-2 subdivisions. No runtime network or system package dependency is required.

Upstream: https://salsa.debian.org/iso-codes-team/iso-codes

Copyright © 2001–2008 Alastair McKinstry; © 2004–2016 Christian Perrier; © 2005–2024 Dr. Tobias Quathamer. Licensed LGPL-2.1-or-later; see `iso-codes-LICENSE.txt`.

Subdivision names validate explicit region fields; district names are never used to infer municipalities. A small reviewed Czech municipality alias set remains separate because ISO subdivision data is not a municipality gazetteer. Update the geography version when changing this snapshot or its aliases.
