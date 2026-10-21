from dataclasses import dataclass


@dataclass(frozen=True)
class WheelBaseline:
    sbom_record: str
    sbom_components: int
    native_module: str
    native_record: str


WHEELS: dict[str, WheelBaseline] = {
    "cp311-cp311-macosx_11_0_arm64": WheelBaseline(
        "sha256=jhT17y3LeSjbbYdgLKMxmWhKXzEKXBmo5QPXUugMb6E",
        56,
        "cel/cel.cpython-311-darwin.so",
        "sha256=_sd2ytli4s7cFGuvuH5Bno4GjJHaPAkseOU-P4Gdn6E",
    ),
    "cp311-cp311-manylinux_2_17_aarch64": WheelBaseline(
        "sha256=Aw2nddBxIqTtKWB7D8BJksQ2Trl_tYLf6in4tHbZ5Ms",
        55,
        "cel/cel.cpython-311-aarch64-linux-gnu.so",
        "sha256=YNjLg8TxGjF8-Trvwh0nFp5YoN2QVo6Dp_betohXhbo",
    ),
    "cp311-cp311-manylinux_2_17_x86_64": WheelBaseline(
        "sha256=LK4uYs47tFVPP8kuFA_JLke0E_gaHug08jGh9ZAg-jA",
        55,
        "cel/cel.cpython-311-x86_64-linux-gnu.so",
        "sha256=1RXJDi3sYWtwjVAJPGbVvqt6eV-0GfLaZPKyaTWIUH8",
    ),
    "cp315-cp315-manylinux_2_17_x86_64": WheelBaseline(
        "sha256=LK4uYs47tFVPP8kuFA_JLke0E_gaHug08jGh9ZAg-jA",
        55,
        "cel/cel.cpython-315-x86_64-linux-gnu.so",
        "sha256=e5FwuqtzVRBOKr4Wm4CbFeOVECxRT6j-yOMc-aSPYdA",
    ),
}
SDIST_SHA256: str = "bb2b6a2e50094219e4366cf36d342d74140c97414545912b34a7e8b8b95c92e6"
WHEEL_PREFIX: str = "common_expression_language-0.10.0-"
WHEEL_SHA256: dict[str, str] = {
    "cp313-cp313-macosx_10_12_x86_64": (
        "5bc7fd00abd45769815a04dad545a28219ee973c426dc0b39aa6a8c82b97f173"
    ),
    "cp312-cp312-macosx_10_12_x86_64": (
        "ee7359e11da1c057cededf5c332f99c71c94a4e8fe7cf3fe14e9726479d2b6bf"
    ),
    "cp311-cp311-manylinux_2_17_armv7l.manylinux2014_armv7l": (
        "ecc5b78d8136a4728667fc3f67aef8725993ab798a481cb7d615f895ebfd8428"
    ),
    "cp311-cp311-macosx_11_0_arm64": (
        "425af498789ae891f4bdad1f978e2864a79fa0a4b8cd26ddaa47a48a22f9d311"
    ),
    "cp312-cp312-macosx_11_0_arm64": (
        "1def298c20eb1fa91a87f422cc2bf2a7568619ae56acae6b15609b2a29b5f66e"
    ),
    "cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "107ca985fcb66ba383ece12a4e2bea4e977e8b21a642d6e6464e943650430c19"
    ),
    "cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "fd7d7dc3168c6651a8436c2d83495f450ddc374326d2a3023097e83ddec505d5"
    ),
    "pp311-pypy311_pp73-manylinux_2_17_armv7l.manylinux2014_armv7l": (
        "2b0a603883c839fafbe1fe3a680dfb79184b4c189934137667be18331e8ec230"
    ),
    "cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "0fe7d794056d99d5fca4fb7d99f640b2311a84d850e498507a4edd67244a79af"
    ),
    "cp312-cp312-win_amd64": (
        "0d8c0b951675fc608eff2baa6c3c9f1d49ef3f014f6202ddfb25fb6ff9f2e228"
    ),
    "cp314-cp314-manylinux_2_5_i686.manylinux1_i686": (
        "8ee1f2f3917643dbddf788a38bf2d354f3a65058341d916540e8636d8c7d5840"
    ),
    "cp313-cp313-manylinux_2_5_i686.manylinux1_i686": (
        "72b8b9893b0a6d09cf0eec98407cf36d7f32a9c53d22b5a0567e1b6c56f36a79"
    ),
    "cp313-cp313-macosx_11_0_arm64": (
        "2c72d3b4da0ec5921df0d34e0ac8356d7eb466798327f5407c6daeffcf52c315"
    ),
    "cp315-cp315-manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "dc82d86b252c007cfa6a879048d5b4a53b021563260930132ae16755e53d477e"
    ),
    "cp314-cp314-manylinux_2_17_ppc64le.manylinux2014_ppc64le": (
        "ee71e778f4a8df3ab4c381190a830c1cdfe9c0fb27f5a214da656015c5febd6c"
    ),
    "cp314-cp314t-manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "bba97124d976c2ed93b79c6def74abde1f423fe1493ac0e19918c9c934c30dc7"
    ),
    "cp312-cp312-manylinux_2_17_s390x.manylinux2014_s390x": (
        "4785d1c01cc8f786ff59ea26acee6f2b02d1a2e89e8d818daad4921de1e0dfed"
    ),
    "cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64": (
        "ef348991adf7df54cee0a2c1a642d5e4e2aa0239bb844719c6454e08d6bd1eaf"
    ),
    "cp314-cp314-manylinux_2_17_s390x.manylinux2014_s390x": (
        "66b0ec39dd9ae8828e3db4f382778eff51c3060cc54cd04b64b72ef66f6afe21"
    ),
    "cp311-cp311-manylinux_2_17_s390x.manylinux2014_s390x": (
        "943b6881841446beea827355ce10ef4594b66bd39d73e269b76edb696443395c"
    ),
    "cp311-cp311-win_amd64": (
        "c2484b50b2d6a8fa51b188d14b1c24555636564bd0045d2be1d0958b2a761908"
    ),
    "cp311-cp311-manylinux_2_17_ppc64le.manylinux2014_ppc64le": (
        "db90be33fc7918c3377dae8c862a3c150a285acbf29724197f0e53c5890c7d95"
    ),
    "cp313-cp313-win_amd64": (
        "fe5192baab821a5f177df395b59b5ff55bce679944801dbc90855b32b39e06bb"
    ),
    "pp311-pypy311_pp73-manylinux_2_17_aarch64.manylinux2014_aarch64": (
        "7968993ff66125a217abef387b8242d581117b97ed5857ff4e5784bce1a22aa1"
    ),
    "pp311-pypy311_pp73-manylinux_2_17_s390x.manylinux2014_s390x": (
        "cd91c42eb839dd564bba9cba056c06be3dfee0eed494da6210d178f66108a773"
    ),
    "cp311-cp311-macosx_10_12_x86_64": (
        "29417ed47959aab5bad3ec12b21b3d332be0008043b8a1ea6c97e262dc41b7dd"
    ),
    "cp314-cp314-manylinux_2_17_aarch64.manylinux2014_aarch64": (
        "6f59915b912797799e690fb4d736cfeb67560ed7538718b638b79dd4a6fa2c00"
    ),
    "cp313-cp313-manylinux_2_17_ppc64le.manylinux2014_ppc64le": (
        "84c280e5d72f298c212f76530b76cde9b65bdf22cee7578f169c0aed2067b11b"
    ),
    "pp311-pypy311_pp73-manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "7ea8ca4d6d40beaee16e0c7302666c6deb26050ae05b53fe0b3fb51c3c49588e"
    ),
    "cp313-cp313-manylinux_2_17_s390x.manylinux2014_s390x": (
        "18bfe13195a507619129c34bfe9915c8553d272369416907733475567c72b51f"
    ),
    "cp312-cp312-manylinux_2_5_i686.manylinux1_i686": (
        "a6ea367d75be0ec7850f8ddbb8b540772a7f0da02625940c98f82de011379761"
    ),
    "cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "f8a18a28c53fa3c4fbb6ba46e50e66cbd555b3a5153920481dc4643509a6ad99"
    ),
    "pp311-pypy311_pp73-manylinux_2_5_i686.manylinux1_i686": (
        "21ba14368bbab920b768c4d5d5643e21a46f80a1a660c677dd2490ceab694923"
    ),
    "cp314-cp314t-manylinux_2_5_i686.manylinux1_i686": (
        "631d9f9b43cd94e6371f2274d4ce22ceff753466642bcf3c4d6f54ae9d5b7419"
    ),
    "cp312-cp312-manylinux_2_17_ppc64le.manylinux2014_ppc64le": (
        "641c4a0eec9c89cee738f667e9d4544f564f761524ce3e4f57b178f4fbfa7784"
    ),
    "pp311-pypy311_pp73-manylinux_2_17_ppc64le.manylinux2014_ppc64le": (
        "8b0e895747aaf19d1adecbcbf51949ab609a295464b11f9f4fbd63099b3e2fec"
    ),
    "cp314-cp314t-manylinux_2_17_aarch64.manylinux2014_aarch64": (
        "ce269beb34ef60d96df5d3308e034316006e5c7437edfeb35e4232abeec49a9c"
    ),
    "cp314-cp314t-manylinux_2_17_ppc64le.manylinux2014_ppc64le": (
        "18ff810c5163fa0768f640e0e7aa7e8801337638e2dece27b7be348bcff217e6"
    ),
    "cp312-cp312-manylinux_2_17_armv7l.manylinux2014_armv7l": (
        "114663998d58a7d1e0a4a02c0e056304ae84acd4e52e1171e466abdd6279f526"
    ),
    "cp314-cp314t-manylinux_2_17_s390x.manylinux2014_s390x": (
        "15f2ef0ff6b9700f799662d954099843b202e42932da9f8fbc9bbcc8b36d94c6"
    ),
    "cp315-cp315t-manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "17aca6731f73a2346b34bd68dbc75b89168d9899fb3fe68611eef2e4c51e7800"
    ),
    "cp311-cp311-win32": (
        "5164b2b49b4f8e50cab9b4192fe6df75fe027d01f7f3c664b03b255f07fd431c"
    ),
    "cp314-cp314-macosx_10_12_x86_64": (
        "c7e992a221921e02cc7fe30688c5256d727316d7e197bf13c4a27d6c75fc069c"
    ),
    "cp314-cp314t-manylinux_2_17_armv7l.manylinux2014_armv7l": (
        "9a86b5b56584c2230bd8854c90efb6d31fffaacc31a69e83d8764f44e1ada152"
    ),
    "cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64": (
        "41c843d3d170c516520e95cfaca7a48df4a07d1c8c16fa9dd76f945b726a7095"
    ),
    "cp315-cp315t-manylinux_2_5_i686.manylinux1_i686": (
        "7d58736a10c84cecb48273ea9234d8873e00dca0e54d6f9657e80647980fe06c"
    ),
    "cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64": (
        "bd3f27cc10659267d6e74c204a74486f760e068b9352045303816b25e5bf06e7"
    ),
    "cp314-cp314-macosx_11_0_arm64": (
        "5f8a6bbd98fcc3df2a1fba28316d9b052f9a20c284367f0a394b4ca9a54bfff6"
    ),
    "cp314-cp314-win_amd64": (
        "95605ae30dbf2a1e4f49e3e416996292a6e2430ce6bb3d5befa5d4e78cbdd226"
    ),
    "cp313-cp313-manylinux_2_17_armv7l.manylinux2014_armv7l": (
        "346f43e55fe951de32ac7f5f9d7db22ac2d75fb7fe98afa27e004ee7f369e933"
    ),
    "cp314-cp314-manylinux_2_17_armv7l.manylinux2014_armv7l": (
        "0459047debc0ac3ea10a4df0d5b5c147f956be34324e44812ed71290a3e3302e"
    ),
    "cp315-cp315-manylinux_2_5_i686.manylinux1_i686": (
        "2d589ee0c8f9347b191fdd5648ad22e9c4c2568b96a082f41fba34e4be256950"
    ),
    "cp311-cp311-manylinux_2_5_i686.manylinux1_i686": (
        "71bfd04a5011a51334a2d40b552942fdd41af2443c77a87aabd1842f5ea7d2fd"
    ),
}
