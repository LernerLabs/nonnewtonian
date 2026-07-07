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
    for s in [slugify("///"), slugify(".."), slugify("你好"), slugify("a/b")]:
        assert s and "/" not in s and ".." not in s  # non-blank, never path-like


def test_nonlatin_names_get_distinct_stable_slugs():
    # The M4 review: all-non-Latin names must NOT collapse to one slug.
    a, b = slugify("吴健雄"), slugify("李政道")
    assert a != b
    assert a != "x" and b != "x"
    assert slugify("吴健雄") == a  # stable across calls
