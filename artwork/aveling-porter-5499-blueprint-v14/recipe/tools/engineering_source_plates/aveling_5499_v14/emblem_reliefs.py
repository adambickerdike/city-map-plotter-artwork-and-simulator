"""Sculpted features drafted against the GWRA lot 51 photograph, in its pixels.

Coordinates refer to the unrotated 1980 x 1529 source. Curves follow casting
relief, not scratches, corrosion or specular highlight boundaries. Features
are named so the reference overlay and final export can be inspected together.
"""
from .drafting import Path,ellipse

def relief_paths():
    return [
        ('near-ear-rim',Path(235,168).curve(241,145,258,123,274,133).curve(293,144,307,163,313,180).curve(286,171,258,171,235,168).vector()),
        ('far-ear-fold',Path(154,189).curve(168,189,181,199,188,216).vector()),
        ('forelock',Path(335,129).curve(361,166,384,219,413,273).curve(421,287,425,306,423,317).vector()),
        ('brow-and-eye',Path(237,315).curve(255,280,288,273,313,301).curve(295,336,260,346,237,315).vector()),
        ('pupil',ellipse(276,307,6,6)),
        ('tear-channel',Path(249,351).curve(241,372,244,390,250,403).vector()),
        ('cheek-and-jaw',Path(344,236).curve(397,288,368,340,333,370).curve(311,391,303,431,312,475).vector()),
        ('nasal-ridge',Path(218,325).curve(205,367,197,416,206,465).vector()),
        ('nostril',Path(240,492).curve(233,482,237,470,248,470).curve(260,469,267,480,261,490).curve(256,498,246,499,240,492).vector()),
        ('muzzle',Path(210,502).curve(225,515,248,524,267,525).curve(279,525,286,519,292,509).vector()),
        ('poll-lock',Path(443,67).curve(464,87,482,117,488,148).vector()),
        ('crest-lock',Path(509,78).curve(563,95,600,144,604,202).vector()),
        ('mane-upper-flow',Path(634,170).curve(629,235,601,290,584,352).curve(576,387,586,417,607,437).vector()),
        ('mane-upper-curl',Path(674,220).curve(681,280,640,310,631,357).curve(623,386,630,409,650,423).vector()),
        ('mane-upper-tip',Path(714,284).curve(721,331,702,370,676,391).vector()),
        ('mane-long-wave',Path(775,414).curve(733,459,687,478,650,502).curve(582,547,544,597,519,649).curve(502,685,500,724,513,750).vector()),
        ('mane-middle-wave',Path(811,452).curve(767,499,718,521,674,550).curve(609,592,571,646,572,702).curve(572,727,580,746,592,764).vector()),
        ('mane-lower-wave',Path(831,490).curve(793,531,749,564,722,600).curve(700,631,700,661,714,692).curve(728,719,746,738,767,750).vector()),
        ('mane-middle-curl',Path(736,550).curve(698,581,680,611,681,643).curve(683,653,687,660,691,665).vector()),
        ('mane-lowest-curl',Path(808,554).curve(780,580,771,609,783,650).vector()),
        ('throat-relief',Path(469,324).curve(487,443,450,557,422,662).curve(403,728,391,789,396,845).vector()),
        ('pectoral-relief',Path(494,797).curve(554,809,610,848,638,894).curve(660,931,658,970,640,1007).vector()),
        ('shoulder-fold',Path(682,790).curve(714,846,718,902,698,948).curve(684,978,686,1003,704,1019).vector()),
        ('belly-relief',Path(751,1081).curve(858,1091,1000,1054,1115,1000).vector()),
        ('haunch-relief',Path(1075,818).curve(1111,747,1157,708,1223,711).curve(1311,715,1401,803,1411,882).curve(1418,941,1408,984,1434,1023).vector()),
        ('near-hind-leg-fold',Path(1185,807).curve(1196,875,1192,950,1229,1012).curve(1267,1090,1402,1152,1500,1144).curve(1550,1145,1575,1189,1546,1232).curve(1508,1295,1443,1341,1388,1368).vector()),
        ('far-hock',Path(1220,1080).curve(1250,1135,1305,1167,1363,1190).curve(1385,1196,1387,1217,1371,1236).curve(1322,1292,1260,1338,1209,1377).vector()),
        ('far-foreleg-tendon',Path(326,936).curve(263,959,191,974,147,995).curve(120,1008,108,1050,98,1091).line(77,1177).vector()),
        ('far-elbow-fold',Path(325,976).curve(348,993,365,1014,372,1036).vector()),
        ('far-fore-hoof-joint',Path(57,1271).curve(77,1246,87,1220,112,1211).vector()),
        ('near-foreleg-tendon',Path(625,1046).curve(601,1087,525,1104,425,1110).line(318,1115).curve(283,1118,267,1137,258,1158).line(223,1339).vector()),
        ('near-elbow-fold',Path(628,1085).curve(604,1125,548,1137,490,1137).vector()),
        ('near-fore-hoof-joint',Path(194,1410).curve(214,1387,224,1360,256,1351).vector()),
        ('far-hind-hoof-joint',Path(1178,1408).curve(1195,1418,1221,1432,1241,1437).vector()),
        ('near-hind-hoof-joint',Path(1393,1395).curve(1410,1408,1431,1417,1445,1424).vector()),
        ('tail-upper-ridge',Path(1503,482).curve(1570,465,1650,489,1702,526).curve(1749,560,1753,627,1762,675).curve(1775,744,1872,784,1930,816).vector()),
        ('tail-inner-flow',Path(1605,552).curve(1650,585,1658,656,1694,711).curve(1733,776,1867,835,1925,867).vector()),
        ('tail-tip-strand-a',Path(1817,850).curve(1851,873,1876,889,1882,907).vector()),
        ('tail-tip-strand-b',Path(1748,822).curve(1778,841,1806,865,1824,886).vector()),
    ]
