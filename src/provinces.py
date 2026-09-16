"""Les 77 provinces de Thailande -> slug RentHub. Seule liste de provinces du
projet: le scraper par lieu la parcourt, le parseur HTML en derive les noms
affiches dans les adresses. Module sans dependance pour eviter tout cycle
d'import entre scraper et parser."""
from __future__ import annotations

# Les 77 provinces officielles de Thaïlande -> slug utilisé par renthub.
# (la page /en/browse/provinces mélange aussi les quartiers de Bangkok,
# on ne garde que les vraies provinces)
THAI_PROVINCES = {
    "Amnat Charoen": "amnat-charoen", "Ang Thong": "angthong", "Bangkok": "bangkok",
    "Bueng Kan": "bueng-kan", "Buri Ram": "buri-ram", "Chachoengsao": "chachoengsao",
    "Chai Nat": "chainat", "Chaiyaphum": "chaiyaphum", "Chanthaburi": "chanthaburi",
    "Chiang Mai": "chiang-mai", "Chiang Rai": "chiang-rai", "Chonburi": "chonburi",
    "Chumphon": "chumphon", "Kalasin": "kalasin", "Kamphaeng Phet": "kamphaeng-phet",
    "Kanchanaburi": "kanchanaburi", "Khon Kaen": "khon-kaen", "Krabi": "krabi",
    "Lampang": "lamphang", "Lamphun": "lamphun", "Loei": "loei", "Lopburi": "lopburi",
    "Mae Hong Son": "mae-hong-son", "Maha Sarakham": "maha-sarakham", "Mukdahan": "mukdahan",
    "Nakhon Nayok": "nakhon-nayok", "Nakhon Pathom": "nakhon-pathom", "Nakhon Phanom": "nakhon-phanom",
    "Nakhon Ratchasima": "nakhon-ratchasima", "Nakhon Sawan": "nakhon-sawan",
    "Nakhon Si Thammarat": "nakhon-sri-thammarat", "Nan": "nan", "Narathiwat": "narathiwat",
    "Nong Bua Lam Phu": "nong-bua-lam-phu", "Nong Khai": "nongkai", "Nonthaburi": "nonthaburi",
    "Pathum Thani": "pathumthani", "Pattani": "pattani", "Phang Nga": "phangnga",
    "Phatthalung": "phatthalung", "Phayao": "phayao", "Phetchabun": "phetchabun",
    "Phetchaburi": "petchburi", "Phichit": "phichit", "Phitsanulok": "phitsanulok",
    "Phra Nakhon Si Ayutthaya": "phra-nakhon-sri-ayutthaya", "Phrae": "phrae", "Phuket": "phuket",
    "Prachin Buri": "prachinburi", "Prachuap Khiri Khan": "prachaubkirikhan", "Ranong": "ranong",
    "Ratchaburi": "ratchburi", "Rayong": "rayong", "Roi Et": "roi-et", "Sa Kaeo": "srakaeo",
    "Sakon Nakhon": "sakon-nakhon", "Samut Prakan": "samut-prakarn", "Samut Sakhon": "samut-sakhon",
    "Samut Songkhram": "samut-songkram", "Saraburi": "saraburi", "Satun": "satun",
    "Si Sa Ket": "si-sa-ket", "Sing Buri": "singburi", "Songkhla": "songkhla", "Sukhothai": "sukhothai",
    "Suphan Buri": "suphanburi", "Surat Thani": "surat-thani", "Surin": "surin", "Tak": "tak",
    "Trang": "trang", "Trat": "trat", "Ubon Ratchathani": "ubon-ratchathani", "Udon Thani": "udon-thani",
    "Uthai Thani": "uthai-thani", "Uttaradit": "uttaradit", "Yala": "yala", "Yasothon": "yasothon",
}
