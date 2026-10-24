from slug import slugify


def test_basic_slug():
    assert slugify("Hello World") == "hello-world"


def test_punctuation_is_collapsed():
    assert slugify("Hello,   World!") == "hello-world"
