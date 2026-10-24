# Empty CSV columns are dropped, so rows misalign

`parse_row("a,b,,d")` returns `["a", "b", "d"]` instead of
`["a", "b", "", "d"]`.

The import job relies on column positions, so every row after an empty cell is
shifted. The helper is `parse_row` in `csvlite.py`.
