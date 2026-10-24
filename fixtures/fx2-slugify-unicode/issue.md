# Accented characters are dropped from slugs

`slugify("Café Münster")` returns `caf-mnster`, losing the `é` and `ü`
completely instead of transliterating them.

Expected slug: `cafe-munster`.

The helper is in `slug.py`. Plain ASCII titles are unaffected, which is why
the existing tests pass.
