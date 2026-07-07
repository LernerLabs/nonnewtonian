from nonnewtonian.slugs import slugify


def test_ascii_basic():
    assert slugify("Emmy Noether") == "emmy-noether"
    assert slugify("C.V. Raman") == "c-v-raman"


def test_unicode_names_from_corpus():
    assert slugify("Nguyễn Văn Hiệu") == "nguyen-van-hieu"
    assert slugify("Shin'ichirō Tomonaga") == "shin-ichiro-tomonaga"
    assert slugify("Bhāskara II") == "bhaskara-ii"
    assert slugify("Subrahmanyan Chandrasekhar") == "subrahmanyan-chandrasekhar"


def test_special_letters_ndk_misses():
    assert slugify("Søren") == "soren"
    assert slugify("Paweł") == "pawel"


def test_never_empty_or_pathlike():
    assert slugify("") == "x"
    assert slugify("///") == "x"
    assert slugify("..") == "x"
    assert "/" not in slugify("a/b")
    assert slugify("你好") == "x"  # all-non-ascii falls back, never blank
