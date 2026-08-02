from cardscanr_worldwide.ptcg_chs_dataset_import import collector_parts, image_url, product_type


def test_ptcg_chs_normalization_and_pinned_image_urls() -> None:
    assert collector_parts("297/SV-P") == ("297", "SV-P")
    assert collector_parts("001/153") == ("001", "153")
    assert image_url("abc123", r"img\181\29.png") == (
        "https://raw.githubusercontent.com/duanxr/PTCG-CHS-Datasets/abc123/img/181/29.png"
    )
    assert product_type("1", "扩充包") == "booster_pack"
    assert product_type("3", "活动奖赏包") == "promotional_pack"
