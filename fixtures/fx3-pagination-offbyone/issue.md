# Page 1 skips the first three items

The listing API is supposed to use 1-based pages, but the first page comes back
empty and the second page starts at the fourth item.

For `items = list(range(7))`, `page=1`, `per_page=3` the expected result is
`[0, 1, 2]`, but the caller receives `[3, 4, 5]`.

The helper is `page_items` in `paging.py`.
