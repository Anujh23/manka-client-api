"""Bank identification + keyword dictionaries used across the engine.

Detection order: account IFSC in the statement header -> bank name in the header
region -> most frequent IFSC prefix on page 1. Narrations mention other banks a lot,
so we never trust a plain "first bank name in the text" match.
"""
from __future__ import annotations

import re
from collections import Counter

# IFSC prefix -> (code, display name)
IFSC_BANKS: dict[str, tuple[str, str]] = {
    "HDFC": ("HDFC", "HDFC Bank"), "ICIC": ("ICICI", "ICICI Bank"), "SBIN": ("SBI", "State Bank of India"),
    "UTIB": ("AXIS", "Axis Bank"), "KKBK": ("KOTAK", "Kotak Mahindra Bank"), "IDFB": ("IDFC", "IDFC FIRST Bank"),
    "YESB": ("YES", "Yes Bank"), "AUBL": ("AU", "AU Small Finance Bank"), "INDB": ("INDUSIND", "IndusInd Bank"),
    "FDRL": ("FEDERAL", "Federal Bank"), "BARB": ("BOB", "Bank of Baroda"), "CNRB": ("CANARA", "Canara Bank"),
    "PUNB": ("PNB", "Punjab National Bank"), "UBIN": ("UNION", "Union Bank of India"),
    "CBIN": ("CENTRAL", "Central Bank of India"), "ESFB": ("EQUITAS", "Equitas Small Finance Bank"),
    "AIRP": ("AIRTEL", "Airtel Payments Bank"), "SRCB": ("SARASWAT", "Saraswat Co-op Bank"),
    "BKID": ("BOI", "Bank of India"), "IBKL": ("IDBI", "IDBI Bank"), "IDIB": ("INDIAN", "Indian Bank"),
    "RATN": ("RBL", "RBL Bank"), "BDBL": ("BANDHAN", "Bandhan Bank"), "UCBA": ("UCO", "UCO Bank"),
    "IOBA": ("IOB", "Indian Overseas Bank"), "UJVN": ("UJJIVAN", "Ujjivan Small Finance Bank"),
    "PYTM": ("PAYTM", "Paytm Payments Bank"), "FINO": ("FINO", "Fino Payments Bank"),
    "MAHB": ("BOM", "Bank of Maharashtra"), "SIBL": ("SIB", "South Indian Bank"), "KVBL": ("KVB", "Karur Vysya Bank"),
    "CIUB": ("CUB", "City Union Bank"), "KARB": ("KARNATAKA", "Karnataka Bank"), "DCBL": ("DCB", "DCB Bank"),
    "JSFB": ("JANA", "Jana Small Finance Bank"), "SURY": ("SURYODAY", "Suryoday Small Finance Bank"),
    "ESMF": ("ESAF", "ESAF Small Finance Bank"), "UTKS": ("UTKARSH", "Utkarsh Small Finance Bank"),
    "DBSS": ("DBS", "DBS Bank India"), "HSBC": ("HSBC", "HSBC India"), "SCBL": ("SCB", "Standard Chartered"),
    "CITI": ("CITI", "Citibank"), "TMBL": ("TMB", "Tamilnad Mercantile Bank"), "JAKA": ("JK", "J&K Bank"),
    "PSIB": ("PSB", "Punjab & Sind Bank"), "NSPB": ("NSDL", "NSDL Payments Bank"), "JIOP": ("JIO", "Jio Payments Bank"),
    "IPOS": ("IPPB", "India Post Payments Bank"), "CSBK": ("CSB", "CSB Bank"), "DLXB": ("DHANLAXMI", "Dhanlaxmi Bank"),
    "SMCB": ("SHIVALIK", "Shivalik Small Finance Bank"), "CLBL": ("CAPITAL", "Capital Small Finance Bank"),
    "NESF": ("NESFB", "North East Small Finance Bank"), "COSB": ("COSMOS", "Cosmos Co-op Bank"),
    "TJSB": ("TJSB", "TJSB Sahakari Bank"), "SVCB": ("SVC", "SVC Co-op Bank"), "ABHY": ("ABHYUDAYA", "Abhyudaya Co-op Bank"),
    "NKGS": ("NKGSB", "NKGSB Co-op Bank"), "KCCB": ("KALUPUR", "Kalupur Co-op Bank"), "APMC": ("APMAHESH", "AP Mahesh Co-op Bank"),
    "STCB": ("SBM", "SBM Bank India"),  # STCB = legacy State Bank of Mauritius (SBMY is State Bank of Mysore)
    "BACB": ("BASSEIN", "Bassein Catholic Co-op Bank"), "TBSB": ("TBSB", "Thane Bharat Sahakari Bank"),
    "DNSB": ("DNSB", "Dombivli Nagari Sahakari Bank"), "VVSB": ("VASAIVIKAS", "Vasai Vikas Sahakari Bank"),
    "KJSB": ("KJSB", "Kalyan Janata Sahakari Bank"),
}

# ---------------------------------------------------------------- full registry: every Indian IFSC prefix
# Public reference data only: the RBI/NPCI IFSC master as published in the Razorpay IFSC dataset
# (github.com/razorpay/ifsc src/IFSC.json, banknames.json, banks.json, sublet.json; fetched 2026-11-13).
# _PREFIXES rows: prefix|code (blank = the prefix itself)|type|name (blank = the dataset has no name for it).
# Banks in the literal map above keep their code and name (stored reports use them). A merged bank keeps its own
# code: an old Allahabad Bank statement is not an Indian Bank layout.
_PREFIXES = """
AANB|||
ABNA||foreign|Royal Bank of Scotland N.V.
ABPB||payments|Aditya Birla Idea Payments Bank
ADBK|||
ADCB||foreign|Abu Dhabi Commercial Bank
ADCC||district_coop|Akola District Central Co-operative Bank
AHDC||district_coop|Ahmednagar District Central Co-operative Bank
AJAR||urban_coop|Ajara Urban Co-operative Bank
AJHC||urban_coop|Ambarnath Jai-hind Co-operative Bank
AKJB||urban_coop|Akola Janata Commercial Co-operative Bank
ALLA||public|Allahabad Bank (merged into Indian Bank)
AMCB||urban_coop|Ahmedabad Mercantile Co-operative Bank
AMDN||urban_coop|Ahmednagar Merchants Co-operative Bank
ANDB||public|Andhra Bank (merged into Union Bank of India)
ANZB||foreign|Australia and New Zealand Banking Group
APBL||state_coop|Andhra Pradesh State Co-operative Bank
APGB||rrb|Andhra Pragathi Grameena Bank
APGV||rrb|Andhra Pradesh Grameena Vikas Bank
APRR||urban_coop|A.P. Raja Rajeswari Mahila Co-operative Urban Bank
ARBL||urban_coop|Arvind Sahakari Bank
ASBL||urban_coop|Apna Sahakari Bank
AUCB||urban_coop|Almora Urban Co-operative Bank
BARA||urban_coop|Baramati Sahakari Bank
BARC||foreign|Barclays Bank
BBKM||foreign|Bank of Bahrein and Kuwait
BCBM||urban_coop|Bharat Co-operative Bank
BCEY||foreign|Bank of Ceylon
BCHN|||
BKDN||public|Dena Bank (merged into Bank of Baroda)
BMBL||public|Bharatiya Mahila Bank (merged into State Bank of India)
BMCB||urban_coop|Bombay Mercantile Co-operative Bank
BNPA||foreign|BNP Paribas Bank
BNSB||urban_coop|Bhagini Nivedita Sahakari Bank Pune
BOFA||foreign|Bank of America
BOTM||foreign|MUFG Bank
BUCB||district_coop|THE BURDWAN CENTRAL CO OPERATIVE BANK LTD
CCBL||urban_coop|Citizen Credit Co-operative Bank
CCIL||institution|Clearing Corporation of India
CCOB||urban_coop|City Co-operative Bank
CHAS||foreign|JP Morgan Chase Bank NA
COAS||local_area|Coastal Local Area Bank
CORP||public|Corporation Bank (merged into Union Bank of India)
CRES||foreign|Credit Suisse AG
CRGB||rrb|Chhattisgarh Rajya Gramin Bank
CRLY||foreign|Credit Agricole Corporate and Investment Bank
CRUB||urban_coop|Shri Chhatrapati Rajashri Shahu Urban Co-operative Bank
CSBX||urban_coop|Chartered Sahakari Bank Niyamitha
CTBA||foreign|Commonwealth Bank of Australia
CTCB||foreign|Chinatrust Commercial Bank
DCUB||urban_coop|Darussalam Co-operative Urban Bank
DEOB||urban_coop|Deogiri Nagari Sahakari Bank Aurangabad
DEUT||foreign|Deutsche Bank
DICG||institution|Deposit Insurance and Credit Guarantee Corporation
DLSC||state_coop|Delhi State Co-operative Bank
DMKJ||urban_coop|Dmk Jaoli Bank
DOHB||foreign|Doha Bank QSC
DURG||urban_coop|Durgapur Steel Peoples Co-operative Bank
EBIL||foreign|Emirates NBD Bank
EIBI||institution|Export Import Bank of India
FIRN||foreign|Firstrand Bank
FSFB||small_finance|Fincare Small Finance Bank (merged into AU Small Finance Bank)
GABK|||
GBCB||urban_coop|Greater Bombay Co-operative Bank
GDCB||district_coop|Gadchiroli District Central Co-operative Bank
GGBK||rrb|Gurgaon Gramin Bank
GSCB||state_coop|Gujarat State Co-operative Bank
GSSB|||
HARC||state_coop|Haryana State Co-operative Apex Bank
HCBL||urban_coop|HASTI Co-operative Bank
HITP|||
HPSC||state_coop|Himachal Pradesh State Co-operative Bank
HUSB||urban_coop|Hutatma Sahakari Bank
HVBK||foreign|Woori Bank
IBBK||foreign|PT Bank Maybank Indonesia TBK
IBKO||foreign|Industrial Bank of Korea
ICBK||foreign|Industrial and Commercial Bank of China
ICLL||institution|Indian Clearing Corporation
IDUK||district_coop|Idukki District Co-operative Bank
INPA|||
ITBL||urban_coop|Irinjalakuda Town Co-operative Bank
JANA|JANASEVA|urban_coop|Janaseva Sahakari Bank, Pune
JASB||urban_coop|Janaseva Sahakari Bank (Borivli)
JJSB||urban_coop|Jalgaon Janata Bank
JPCB||urban_coop|Jalgaon Peoples Co-operative Bank
JSBL||urban_coop|Janakalyan Sahakari Bank
JSBP||urban_coop|Janata Sahakari Bank (Pune)
JTSC||urban_coop|Janatha Seva Co-operative Bank
KACE||district_coop|Kangra Central Co-operative Bank
KAIJ||urban_coop|Kallappanna Awade Ichalkaranji Janata Sahakari Bank
KANG||urban_coop|Kangra Co-operative Bank
KBKB||foreign|Kookmin Bank
KCBL||urban_coop|Kapol Co-operative Bank
KDCB||district_coop|Kozhikode District Co-operative Bank
KGRB||rrb|Kaveri Grameena Bank
KLGB||rrb|Kerala Gramin Bank
KNBL|||
KNSB||urban_coop|Kurla Nagarik Sahakari Bank
KOEX||foreign|KEB Hana Bank
KOLH||urban_coop|Kolhapur Urban Co-operative Bank
KRTH||foreign|Krungthai Bank
KSBK||state_coop|Kerala State Co-operative Bank
KSCB||state_coop|Karnataka State Co-operative Apex Bank
KUCB||urban_coop|Karad Urban Co-operative Bank
KVGB||rrb|Karnataka Vikas Grameena Bank
LAVB||foreign|Laxmi Vilas Bank (merged into DBS Bank India)
LKBL||private|Lord Krishna Bank (merged into HDFC Bank)
LUCB|||
MAHG||rrb|Maharashtra Gramin Bank
MCAB||state_coop|Meghalaya Co-operative Apex Bank
MCBL||urban_coop|Mahanagar Co-operative Bank
MDBK||urban_coop|Model Co-operative Bank
MDCB||district_coop|Mumbai District Central Co-operative Bank
MGRB||rrb|Malwa Gramin Bank
MHCB||foreign|Mizuho Bank
MKPB||urban_coop|Malkapur Urban Co-operative Bank
MSBL||urban_coop|MAHESH SAHAKARI BANK LTD PUNE
MSCI||state_coop|Maharashtra State Co-operative Bank
MSHQ||foreign|Mashreq Bank
MSLM||urban_coop|Muslim Co-operative Bank
MSNU||urban_coop|Mehsana Urban Co-operative Bank
MUBL||urban_coop|Municipal Co-operative Bank
MVCB||urban_coop|Sir M Visvesvaraya Co-operative Bank
NATA||foreign|National Australia Bank
NBAD||foreign|National Bank of Abu Dhabi PJSC
NBRD||institution|National Bank for Agriculture and Development
NCUB||urban_coop|Nilambur Co-operative Urban Bank
NDVS|||
NGSB||urban_coop|Nagpur Nagarik Sahakari Bank
NHBA|||
NICB||urban_coop|New India Co-operative Bank
NJBK||urban_coop|Nav Jeevan Co-operative Bank
NMCB||urban_coop|Nasik Merchants Co-operative Bank
NMGB||rrb|North Malabar Gramin Bank
NNSB||urban_coop|Nutan Nagarik Sahakari Bank
NOSC||foreign|Bank of Nova Scotia
NTBL||private|Nainital Bank
NUCB||urban_coop|Nagar Urban Co-operative Bank
NVNM||urban_coop|Navnirman Co-operative Bank
ORBC||public|Oriental Bank of Commerce (merged into Punjab National Bank)
ORCB||state_coop|Odisha State Co-operative Bank
PHNP|||
PJSB||urban_coop|Gopinath Patil Parsik Janata Sahakari Bank
PKGB||rrb|Karnataka Gramin Bank
PMCB||small_finance|Punjab & Maharashtra Co-operative Bank (merged into Unity Small Finance Bank)
PMEC||urban_coop|Prime Co-operative Bank
PPNT|||
PRTH||rrb|Prathama Bank
PSBL||urban_coop|Pavana Sahakari Bank
PUCB||urban_coop|Pandharpur Urban Co-operative Bank
PUSD||urban_coop|Pusad Urban Co-operative Bank
QNBA||foreign|Qatar National Bank
RABO||foreign|Rabobank International
RBIH||institution|IDRBT
RBIN||institution|Reserve Bank Of India
RBIP||institution|Reserve Bank of India
RBIS||institution|Reserve Bank of India
RDCB||district_coop|Rajnandgaon District Central Co-operative Bank
RMGB||rrb|Rajasthan Marudhara Gramin Bank
RNSB||urban_coop|Rajkot Nagarik Sahakari Bank
RRBP||urban_coop|Rajarambapu Sahakari Bank
RSBL||urban_coop|Rajgurunagar Sahakari Bank
RSCB||state_coop|Rajasthan State Co-operative Bank
RSSB||urban_coop|Rajarshi Shahu Sahakari Bank
SABR||foreign|SBER Bank
SAHE||urban_coop|Sahebrao Deshmukh Co-operative Bank
SANT||urban_coop|Sant Sopankaka Sahakari Bank
SBBJ||public|State Bank of Bikaner and Jaipur (merged into State Bank of India)
SBHY||public|State Bank of Hyderabad (merged into State Bank of India)
SBLS||urban_coop|Samarth Sahakari Bank
SBMY||public|State Bank of Mysore (merged into State Bank of India)
SBTR||public|State Bank of Travancore (merged into State Bank of India)
SDCB||district_coop|Surat District Co-operative Bank
SDCE||district_coop|Satara District Central Co-operative Bank
SECB||urban_coop|Sree Charan Souhardha Co-operative Bank
SGBA||rrb|Saurashtra Gramin Bank
SHBK||foreign|Shinhan Bank
SIDB||institution|Small Industries Development Bank Of India
SIDC||district_coop|Sindhudurg District Central Co-operative Bank
SJSB||urban_coop|Solapur Janata Sahakari Bank
SKNB||urban_coop|Shree Kadi Nagarik Sahakari Bank
SKSB||urban_coop|Shikshak Sahakari Bank
SMBC||foreign|Sumitomo Mitsui Banking Corporation
SMNB||urban_coop|Smriti Nagrik Sahakari Bank
SNBK||urban_coop|Saraspur Nagrik Co-operative Bank
SOGE||foreign|Societe Generale
SPCB||urban_coop|Surat People's Co-operative Bank
STBP||public|State Bank of Patiala (merged into State Bank of India)
SUNB||urban_coop|Surat National Co-operative Bank
SUSB||urban_coop|Suco Souharda Sahakari Bank
SUTB||urban_coop|Sutex Co-operative Bank
SVBL||urban_coop|Seva Vikas Co-operative Bank
SVSH||urban_coop|Shree Veershaiv Co-operative Bank
SYNB||public|Syndicate Bank (merged into Canara Bank)
TAUB||urban_coop|Ankola Urban Co-operative Bank
TBMC||urban_coop|Banaskantha Mercantile Co-operative Bank
TCBR||urban_coop|The Cooperative Bank Of Rajkot Limited
TCUB||urban_coop|Trivandrum Co-operative Urban Bank
TDCB||district_coop|Thane District Central Co-operative Bank
TGMB||urban_coop|Tumkur Grain Merchant's Co-operative Bank
TGRB||rrb|TELANGANA GRAMEENA BANK
THRS||district_coop|Thrissur District Co-operative Bank
TMSB||urban_coop|The Malad Sahakari Bank
TNCB||urban_coop|Nawanagar Co-operative Bank
TNGB||rrb|Tamil Nadu Grama Bank
TNSC||state_coop|Tamilnadu State Apex Co-operative Bank
TPSC||state_coop|Punjab State Co-operative Bank
TSAB||district_coop|Telangana State Co-operative Apex Bank
TSSB||urban_coop|Satara Sahakari Bank
TTCB||urban_coop|Textile Traders Co-operative Bank
UCBS||urban_coop|Utkal Co-operative Banking Society
UCLB|||
UNBA||small_finance|Unity Small finance Bank Limited
UOVB||foreign|United Overseas Bank
UPCB||urban_coop|Uttar Pradesh Co-operative Bank
URBN||urban_coop|Urban Co-operative Bank Perinthalmanna
USCB||state_coop|UTTARAKHAND STATE COOPERATIVE BANK LIMITED
UTBI||public|United Bank of India (merged into Punjab National Bank)
UUCB||urban_coop|Udaipur Urban Co-operative Bank
VARA||urban_coop|Varachha Co-operative Bank
VASJ||urban_coop|Vasai Janata Sahakari Bank
VCOB||urban_coop|The Vijay Co-operative Bank
VIJB||public|Vijaya Bank (merged into Bank of Baroda)
VSBA|||
VSBL||urban_coop|Vishweshwar Sahakari Bank
VTBJ||foreign|VTB Bank
VYSA||private|ING Vysya Bank (merged into Kotak Mahindra Bank)
WBSC||state_coop|West Bengal State Co-operative Bank
WPAC||foreign|Westpac Banking Corporation
XNSE||institution|NSE Clearing Limited
ZCBL||urban_coop|Zoroastrian Co-operative Bank
ZSBL||district_coop|Zila Sahakari Bank Ghaziabad
"""
_MEMBER_IFSC = """
APBL0001001|TSDX|district_coop|District Co-operative Central Bank Srikakul
APBL0002001|TVDX|district_coop|District Co-operative Central Bank Vizianagaram
APBL0003001|DICX|district_coop|District Co-operative Central Bank Visakhapatnam
APBL0004001|TDIX|district_coop|District Co-operative Central Bank Kakinada
APBL0005001|DCDX|district_coop|District Central Co-operative Bank Elluru
APBL0006001|KRDX|district_coop|Krishna District Co-operative Bank
APBL0007001|GDCX|district_coop|Guntur District Co-operative Central Bank
APBL0008001|TPDX|district_coop|Prakasam District Co-operative Central Bank
APBL0009002|PSRX|urban_coop|Sri Potti Sriramulu Nellore Dccb
APBL0010001|CHDX|district_coop|Chittoor District Co-operative Central Bank
APBL0011001|KAAX|district_coop|Kadappa District Co-operative Central Bank
APBL0012001|TADX|district_coop|Anantapur District Co-operative Central Bank
APBL0013001|DCKX|district_coop|District Co-operative Central Bank Kurnool
APBL0014001|DCCX|district_coop|District Co-operative Central Bank Mahabubnagar
APBL0015022|HDCX|district_coop|Hyderabad District Co-operative Bank
APBL0017001|MEDX|district_coop|District Co-operative Central Bank
APBL0019001|ADDX|district_coop|Adilabad District Co-operative Central Bank
APBL0020001|KDBX|district_coop|Karimnagar District Co-operative Central Bank
APBL0021001|WARX|district_coop|Warangal District Co-operative Central Bank
APBL0022001|DCEX|district_coop|District Central Co-operative Bank Khammam
APBL0023001|NLGX|district_coop|Nalgonda Dist. Co-operative Central Bank
BARB0BGGBXX|BGGX|rrb|Baroda Gujarat Gramin Bank
BARB0BUPGBX|BUGX|rrb|Baroda Uttar Pradesh Gramin Bank
BKID000DCBL|DHKX|urban_coop|Dhakuria Co-operative Bank
BKID099BKCB|BKCX|urban_coop|Bhavasara Kshatriya Co-operative Bank
BKID0NAMRGB|NJGX|rrb|Narmada Jhabua Gramin Bank
BKID0WAINGB|WKGX|rrb|Vidharbha Kokan Gramin Bank
CBIN0CGDCBN|CRSX|state_coop|Chhattisgarh Rajya Sahakari Bank Mydt
CBIN0MPABAA|MPRX|district_coop|Madhya Pradesh Rajya Sahakari Bank Maryadit
CBIN0MPDACT|JIMX|district_coop|Jila Sahakari Kendriya Bank Mydt Mandla
CBIN0MPDCAB|JIBX|district_coop|Jila Sahakari Kendriya Bank Maryadit Balaghat
CBIN0MPDCAC|JMBX|district_coop|Jila Sahakari Kendriya Bank Maryadit Betul
CBIN0MPDCAD|JBHX|district_coop|Jila Sahakari Kendriya Bank Maryadit Bhind
CBIN0MPDCAE|BHOX|district_coop|Bhopal Co-operative Central Bank
CBIN0MPDCAF|JCHX|district_coop|Jila Sahakari Kendriya Bank Mydt Chhatarpur
CBIN0MPDCAG|JICX|district_coop|Jila Sahakari Kendriya Bank Maryadit Chhindwara
CBIN0MPDCAH|JIDX|district_coop|Jila d Kendriya Bank Mydt Damoh
CBIN0MPDCAI|JMDX|district_coop|Jila d Kendriya Bank Mydt Datia
CBIN0MPDCAJ|JDEX|district_coop|Jila Sahakari Kendriya Bank Mydtt Dewas
CBIN0MPDCAK|JSDX|district_coop|Jila Sahakari Kendriya Bank Mydt Dhar
CBIN0MPDCAL|JIGX|district_coop|Jila Sahakari Kendriya Bank Mydt Guna
CBIN0MPDCAM|JGWX|district_coop|Jila Sahakari Bank Mydt. Gwalior
CBIN0MPDCAN|JHSX|district_coop|Jila Sahakari Kendriya Bank Mydt Hoshangabad
CBIN0MPDCAO|IPCX|district_coop|Indore Premier Co-operative Bank
CBIN0MPDCAP|JKDX|district_coop|Jila Sahakari Kendriya Bank Mydt. Jabalpur
CBIN0MPDCAQ|JJHX|district_coop|Jila d Kendriya Bank Mydt Jhabua
CBIN0MPDCAR|JIKX|district_coop|Jila Sahakari Kendariya Bank Mydt Khandwa
CBIN0MPDCAS|JKHX|district_coop|Jila Kendriya Bank Maryadit Khargone
CBIN0MPDCAU|JMAX|district_coop|Jila Sahakari Kendriya Bank Mydt Mandsaur
CBIN0MPDCAV|JMOX|district_coop|Jila Sahakari Kendriya Bank Mydt Morena
CBIN0MPDCAW|JNAX|district_coop|Jila Sahakari Kendriya Bank Mydt Narsinghpur
CBIN0MPDCAX|JPAX|district_coop|Jila Sahakari Kendriya Bank Mydt Panna
CBIN0MPDCAY|JKRX|district_coop|Jilla Sahakari Kendriya Bank Mydt Raisen
CBIN0MPDCAZ|JIRX|district_coop|Jila Sahakari Kendriya Bank Maryadit Rajgarh
CBIN0MPDCBA|JRAX|district_coop|Jila Sahakari Kendriya Bank Mydt Ratlam
CBIN0MPDCBB|JSRX|district_coop|Jila Sahakari Kendriya Bank Mydt Rewa
CBIN0MPDCBC|JBMX|district_coop|Jila Sahakari Kendriya Bank Maryadit Sagar
CBIN0MPDCBD|JSTX|district_coop|Jila Sahakari Kendriya Bank Mydt Satna
CBIN0MPDCBE|JSEX|district_coop|Jila Sahakari Kendriya Bank Mydt Sehore
CBIN0MPDCBF|JSOX|district_coop|Jila Sahakari Kendriya Bank Myt Seoni
CBIN0MPDCBG|JIOX|district_coop|Jila Sahakari Kendriya Bank Mydt Shahdol
CBIN0MPDCBH|JSHX|district_coop|Jila Sahakari Kendriya Bank Mydt Shajapur
CBIN0MPDCBI|JSVX|district_coop|Jila Sahakari Kendriya Bank Maryadit Shivpuri
CBIN0MPDCBJ|JISX|district_coop|Jila Sahakari Kendriya Bank Maryadit Sidhi
CBIN0MPDCBK|JTIX|district_coop|Jila Sahakari Kendriya Bank Mydt Tikamgarh
CBIN0MPDCBL|JRKX|district_coop|Jila Sahakari Kendriya Bank Mydt Ujjain
CBIN0MPDCBM|JLSX|district_coop|Jila Sahakari Kendriya Bank Mydt Vidisha
CBIN0R10001|UBGX|rrb|Uttar Bihar Gramin Bank
CBIN0R40012|UKGX|rrb|Uttarbanga Kshetriya Gramin Bank
DEOB0000001|DEOX|district_coop|Deogiri Sahakari Bank Aurangabad
FDRL012FCUB|FEKX|urban_coop|Feroke Co-operative Bank
FDRL01BMCOB|BHJX|urban_coop|Bhuj Mercentile Co-operative Bank
FDRL01MEUCB|MEUX|urban_coop|Meenachil East Urban Co-operative Bank
FDRL0AUCB02|AWCX|urban_coop|Alwaye Urban Co-operative Bank
FDRL0CCUB01|CALX|district_coop|Calicut Co-operative Urban Bank
FDRL0NCUB01|NCUX|urban_coop|Nilambur Co-operative Urban Bank
FDRL0WDCB01|WDCX|district_coop|Wayanad District Co-operative Bank
GSCB0ADC001|ADBX|district_coop|Ahmedabad District Co-operative Bank
GSCB0AMR001|AMRX|district_coop|Amreli Jilla Madhyastha Sahakari Bank
GSCB0BKD001|BKDX|district_coop|Banaskantha District Central Co-operative Bank
GSCB0BRC001|BRUX|district_coop|Bharuch District Central Co-operative Bank Bharuc
GSCB0BRD001|BRDX|district_coop|Baroda Central Co-operative Bank
GSCB0BVN001|BVNX|district_coop|Bhavnagar District Co-operative Bank
GSCB0GUMC01|GMCX|urban_coop|Gujarat Mercantile Co-operative Bank
GSCB0JMN001|JCDX|district_coop|Jamnagar District Co-operative Bank
GSCB0JNDC01|JNDX|district_coop|Junagadh Jilla Sahakari Bank
GSCB0KDT001|KOTX|district_coop|Kodinar Taluka Co-operative Banking Union
GSCB0KTC001|KACX|district_coop|Kachchh District Central Co-operative Bank
GSCB0MSN001|MSNX|district_coop|Mehsana District Central Co-operative Bank
GSCB0PDC001|PANX|district_coop|Panchmahal District Co-operative Bank
GSCB0RJT001|RJTX|district_coop|Shri Rajkot District Co-operative Bank
GSCB0SKB001|SADX|district_coop|Sabarkantha District Central Co-operative Bank
GSCB0SNR001|SUDX|district_coop|Surendranagar District Co-operative Bank
GSCB0UBMCBL|TBMX|urban_coop|Bapunagar Mahila Co-operative Bank
GSCB0UCCCBL|CCMX|urban_coop|Chanasma Commercial Co-operative Bank
GSCB0UGACBL|GACX|urban_coop|Gujarat Ambuja Co-operative Bank
GSCB0UJAMNA|TJNX|urban_coop|Jamnagar Mahila Sahakari Bank
GSCB0UKMCBL|KSMX|urban_coop|Kosamba Mercantile Co-operative Bank
GSCB0UKNSBL|KBNX|urban_coop|Khambhat Nagarik Sahakari Bank
GSCB0UKPCBL|KHDX|urban_coop|Kheda People's Co-operative Bank
GSCB0UMCCBL|MKYX|urban_coop|Marketyard Commercial Co-operative Bank
GSCB0UMNCBL|TNBX|urban_coop|Meghraj Nagarik Sahakari Bank
GSCB0UNNCBL|NNCX|urban_coop|Naroda Nagrik Co-operative Bank
GSCB0UPATAN|PTSX|urban_coop|Patan Nagarik Sahakari Bank
GSCB0UPNSBL|PADX|urban_coop|Padra Nagar Nagrik Sahakari Bank
GSCB0URNSBL|RJNX|urban_coop|Rajpipla Nagarik Sahakari Bank
GSCB0USATHM|TSPX|urban_coop|Sathamba Peoples Co-operative Bank
GSCB0USAURA|TSUX|urban_coop|Saurashtra Co-operative Bank
GSCB0USBNBL|SBNX|urban_coop|Shree Bhavnagar Nagrik Sahakari Bank
GSCB0USHNBL|SINX|urban_coop|Shihori Nagarik Sahakari Bank
GSCB0USMCBL|TSMX|urban_coop|Sardargunj Mercantile Co-operative Bank Patan
GSCB0USSNPL|SALX|urban_coop|Salal Sarvodaya Nagarik Sahakari Bank
GSCB0UTBSBL|TBNS|urban_coop|Becharaji Nagarik Sahakari Bank
GSCB0UTKSBL|TKAX|urban_coop|Karjan Nagrik Sahakari Bank
GSCB0UTPCBL|PGTX|urban_coop|Pragati Co-operative Bank, Thara
GSCB0UTSNBL|SNBX|urban_coop|Sarvodaya Nagrik Sahakari Bank
GSCB0UUCOBL|UNIX|urban_coop|United Co-operative Bank
GSCB0UUNJBL|UJSX|urban_coop|Unjha Nagarik Sahakari Bank
GSCB0UUNSBL|UNSX|urban_coop|Unava Nagrik Sahakari Bank
GSCB0UVIRAM|VIMX|urban_coop|Viramgam Mercantile Co-operative Bank
GSCB0VDC001|VADX|district_coop|Valsad District Central Co-operative Bank
HDFC0C0CUCB|CHBX|urban_coop|Chamba Urban Co-operative Bank Chamba
HDFC0C0RSBL|RSBX|urban_coop|Raigad Sahakari Bank
HDFC0CAACOB|AACX|urban_coop|Akhand Anand Co.op Bank
HDFC0CABBLM|ABBL|foreign|AB Bank
HDFC0CACABL|ACAX|state_coop|Assam Co-operative Apex Bank
HDFC0CACUB9|ACUB|urban_coop|Aryapuram Co-operative Urban Bank
HDFC0CADARS|ACBX|urban_coop|Adarsh Co-operative Bank
HDFC0CALAVI|ALAX|urban_coop|Alavi Co-operative Bank
HDFC0CAMARN|ACOX|urban_coop|Amarnath Co-operative Bank
HDFC0CAMBLA|AKMX|urban_coop|Akola Merchant Co-operative Bank
HDFC0CAMMCO|AMMX|urban_coop|Adarsh Mahila Mercantile Co-operative Bank
HDFC0CAMNSB|AMNX|urban_coop|Astha Mahila Nagrik Sahakari Bank Maryadit
HDFC0CAMSBA|AMSX|urban_coop|Ambika Mahila Sahakari Bank
HDFC0CANSCB|ANSX|state_coop|Andaman & Nicobar State Co-operative Bank
HDFC0CAPANI|APNX|urban_coop|Apani Sahakari Bank
HDFC0CAPCBL|AJPX|urban_coop|Ambajogai Peoples Co-operative Bank
HDFC0CAPJBK|APJX|urban_coop|Ap Janata Co-operative Urban Bank
HDFC0CASB01|DBAX|urban_coop|Dr Babasaheb Ambedkar Sahakari Bank Nasik
HDFC0CAUB01|AGCX|urban_coop|Agrasen Co-operative Urban Bank
HDFC0CAUCBL|ANUX|urban_coop|Anuradha Urban Co-operative Bank
HDFC0CBACOB|BACX|urban_coop|Bihar Awami Co-operative Bank
HDFC0CBCBBK|BCBX|urban_coop|Bantra Co-operative Bank
HDFC0CBCBLH|BALB|urban_coop|Bally Co-operative Bank
HDFC0CBCBMN|BTCX|urban_coop|Balitikuri Co-operative Bank
HDFC0CBCCBL|BURX|district_coop|Burdwan Central Co-operative Bank
HDFC0CBDSCB|BSCX|urban_coop|Baidyabati Sheoraphuli Co-operative Bank
HDFC0CBHLUB|BHUX|urban_coop|Bhilwara Urban Co-operative Bank
HDFC0CBHRAT|SBUJ|urban_coop|Shri Bharat Urban Co-operative Bank Jaysingpur
HDFC0CBHUCB|BUBX|urban_coop|Bhandara Urban Co-operative Bank
HDFC0CBLMSB|BMSX|urban_coop|Bhagyalakshmi Mahila Sahakari Bank
HDFC0CBNB01|BRSX|urban_coop|Baran Nagrik Bank
HDFC0CBNBNK|BNSX|urban_coop|Bhagini Nivedita Sahakari Bank
HDFC0CBNSAH|BHBX|urban_coop|Bhilai Nagarik Sahakari Bank Maryadit
HDFC0CBNSBL|BALX|urban_coop|Balasinor Nagarik Sahakari Bank
HDFC0CBNSBM|BDNX|urban_coop|Bhind Nagrik Sahakari Bank Mydt
HDFC0CBPCBL|BPCX|urban_coop|Bhadgaon People's Co-operative Bank
HDFC0CBSB01|BSBX|urban_coop|Baramati Sahakari Bank
HDFC0CBUCBB|BRMX|urban_coop|Bramhapuri Urban Co-operative Bank
HDFC0CBUCBG|BCUB|urban_coop|Bicholim Urban Co-operative Bank
HDFC0CBUCBL|BJUX|urban_coop|Bijnor Urban Co-operative Bank
HDFC0CBVNSB|BBVX|urban_coop|Bhabhar Vibhag Nagrik Sahakari Bank
HDFC0CBWRUB|BUCX|urban_coop|Beawar Urban Co-operative Bank
HDFC0CCBSBK|SBKX|urban_coop|Sri Channabasavaswamy Souhardha Pattana Sahak Bank
HDFC0CCCB02|COCX|urban_coop|Co-operative City Bank
HDFC0CCMCBL|CMCB|urban_coop|Colour Merchant's Co-operative Bank
HDFC0CCPUBL|PUNX|urban_coop|Pune Urban Co-operative Bank
HDFC0CCUB01|CHIX|urban_coop|Chikhli Urban Co-operative Bank
HDFC0CCUCBL|CTUX|urban_coop|Chittorgarh Urban Co-operative Bank
HDFC0CDACBL|DUCX|urban_coop|Daund Urban Co-operative Bank
HDFC0CDACUB|ACKX|urban_coop|Annasaheb Chougule Urban Co-operative Bank
HDFC0CDILIP|DIUX|urban_coop|Dilip Urban Co-operative Bank
HDFC0CDMCBD|DAHX|urban_coop|Dahod Mercantile Co-operative Bank
HDFC0CDMCBL|TDMX|urban_coop|Dhanera Mercantile Co-operative Bank
HDFC0CDMOBL|DMCX|urban_coop|Durgapur Mahila Co-operative Bank
HDFC0CDNSBA|DNSX|urban_coop|Deendayal Nagari Sahakari Bank
HDFC0CDSBNB|DSBX|urban_coop|Daivadnya Sahakara Bank Niyamit
HDFC0CDUCBE|DHUX|urban_coop|Dahod Urban Co-operative Bank
HDFC0CDUCBL|DCUX|urban_coop|Darussalam Co-operative Urban Bank
HDFC0CECBL2|ECBL|urban_coop|Excellent Co-operative Bank
HDFC0CEENAD|TECX|urban_coop|Eenadu Co-operative Urban Bank
HDFC0CEUCBL|EWCX|urban_coop|Etawah Urban Co-operative Bank Etawah
HDFC0CGCB02|GODX|urban_coop|Godavari Urban Co-operative Bank Nashik
HDFC0CGCBLG|GANX|urban_coop|Gandhidham Co-operative Bank
HDFC0CGNCBL|TGNX|urban_coop|Gandhinagar Nagrik Co-operative Bank
HDFC0CGNSBL|GOSX|urban_coop|Gondal Nagarik Sahakari Bank
HDFC0CGPCBL|GHPX|urban_coop|Ghatal Peoples' Co-operative Bank
HDFC0CGUB02|GUCX|urban_coop|Goa Urban Co-operative Bank
HDFC0CHMCBL|THMX|urban_coop|Halol Mercantile Co-operative Bank
HDFC0CHOKSB|SHOX|urban_coop|Sihor Mercantile Co-operative Bank
HDFC0CHUCBH|HUCX|urban_coop|Hissar Urban Co-operative Bank
HDFC0CICBL1|INDX|urban_coop|Independence Co-operative Bank
HDFC0CIMB01|ICHX|urban_coop|Ichalkaranji Merchants Co-operative Bank
HDFC0CISWAM|ISMX|urban_coop|Indore Swayam Mahila Co-operative Bank
HDFC0CITC01|ITCX|urban_coop|Irinjalakuda Town Co-operative Bank
HDFC0CIUCBL|IUCB|urban_coop|Integral Urban Co-operative Bank
HDFC0CJALOR|JRSX|urban_coop|Jalore Nagrik Sahakari Bank
HDFC0CJBMLG|JCBX|urban_coop|Janata Co-operative Bank Malegaon.
HDFC0CJCBLD|JACX|urban_coop|Jain Co-operative Bank
HDFC0CJCBLN|JKCX|urban_coop|Janakalyan Co-operative Bank Nashik
HDFC0CJCCBL|JUCX|urban_coop|Junagadh Commercial Co-operative Bank
HDFC0CJCUBL|JOWX|urban_coop|Jowai Co-operative Urban Bank
HDFC0CJMCBL|JMCX|urban_coop|Jalna Merchants Co-operative Bank
HDFC0CJMS99|JMSX|urban_coop|Jijamata Mahila Sahakari Bank
HDFC0CJNB08|JONX|urban_coop|Jodhpur Nagrik Sahakari Bank
HDFC0CJNSBB|JNSX|urban_coop|Janseva Nagari Sahakari Bank Marydit
HDFC0CJNSBL|JLWX|urban_coop|Jhalawar Nagrik Sahakari Bank
HDFC0CJNSBM|JHAX|urban_coop|Jharneshwar Nagrik Sahakari Bank Maryadit
HDFC0CJPCBL|JLNX|urban_coop|Jalna Peoples Co-operative Bank Jalna
HDFC0CJSCBL|JASX|urban_coop|Janatha Seva Co-operative Bank
HDFC0CJUCBI|JSWX|urban_coop|Jansewa Urban Co-operative Bank
HDFC0CJUCBL|JMHX|urban_coop|Jamshedpur Urban Co-operative Bank
HDFC0CKAMCO|KASX|urban_coop|Kashmir Mercantile Co-operative Bank
HDFC0CKBS01|KBSX|local_area|Krishna Bhima Samruddhi Local Area Bank
HDFC0CKCCBL|KRIX|urban_coop|Krishnagar City Co-operative Bank
HDFC0CKMB01|KMSX|urban_coop|Kolhapur Mahila Sahakari Bank
HDFC0CKMNSB|KKMX|urban_coop|Kankaria Mainagar Nagrik Sahakari Bank
HDFC0CKNB02|KOSX|urban_coop|Kota Nagrik d Bank Kota
HDFC0CKNSBZ|KDNX|urban_coop|Kodinar Nagrik Sahakari Bank
HDFC0CKOUCB|KOYX|urban_coop|Koylanchal Urban Co-operative Bank
HDFC0CKSBL1|KHNX|urban_coop|Kohinoor Sahakari Bank Ichalkaranji
HDFC0CKSBNB|KSNX|urban_coop|Koteshwara Sahakari Bank Niyamitha
HDFC0CKTCCB|KALX|district_coop|Kalna Town Credit Co-operative Bank
HDFC0CKUB01|KHUX|urban_coop|Khamgaon Urban Co-operative Bank
HDFC0CKUCBK|TKUX|urban_coop|Karan Urban Co-operative Bank
HDFC0CKUCBL|KUCX|urban_coop|Kolhapur Urban Co-operative Bank
HDFC0CKUCBS|KVCX|urban_coop|Kavita Urban Co-operative Bank
HDFC0CKUCPL|KSUX|urban_coop|Kashipur Urban Co-operative Bank
HDFC0CLCBLO|LCBX|urban_coop|Lalbaug Co-operative Bank
HDFC0CLLC02|LICB|urban_coop|Liluah Co-operative Bank
HDFC0CLPCBL|TLPX|urban_coop|Lunawada Peoples Co-operative Bank
HDFC0CLSABL|LONX|urban_coop|Lonavala Sahakari Bank
HDFC0CMAHAD|TASX|urban_coop|Annasaheb Savant Co-operative Urban Bank Mahad
HDFC0CMALAD|MSBL|urban_coop|MAHESH SAHAKARI BANK LTD PUNE
HDFC0CMAN01|ABUX|urban_coop|Abhinandan Urban Co-operative Bank Amravati
HDFC0CMANCB|MSOX|urban_coop|Manorama Co-operative Bank Solapur
HDFC0CMASBL|MHSX|urban_coop|Mahesh Sahakari Bank Pune
HDFC0CMBANK|ANMX|urban_coop|Ahmednagar Merchant's Co-operative Bank
HDFC0CMBCUB|MACX|urban_coop|Mahabhairab Co-operative Urban Bank
HDFC0CMCBLD|MGCX|urban_coop|Mansing Co-operative Bank
HDFC0CMCOBL|MABL|urban_coop|Malleshwaram Co-operative Bank
HDFC0CMCUBL|MCUX|urban_coop|Mahaveer Co-operative Urban Bank
HDFC0CMFUCB|MFUX|urban_coop|Mahatma Fule Urban Co-operative Bank,amravati
HDFC0CMNSAA|SMNX|urban_coop|Shree Mahuva Nagrik Sahakari Bank
HDFC0CMNSBM|MMMX|urban_coop|Mahila Nagrik Sahakari Bank Maryadit Mahasamund
HDFC0CMPLTB|TMAX|urban_coop|Madanapalle Co-operative Town Bank
HDFC0CMPSSB|MVIX|urban_coop|Manvi Pattana Souharda Sahakari Bank Ni
HDFC0CMUCB1|TMUX|urban_coop|Mahila Urban Co-operative Bank
HDFC0CMUCBL|MALX|urban_coop|Malviya Urban Co-operative Bank
HDFC0CMUDCB|MUDX|urban_coop|Madheshwari Urban Development Co-operative Bank
HDFC0CMUSLM|MUSX|urban_coop|Muslim Co-operative Bank
HDFC0CNB311|NIRX|urban_coop|Nirmal Urban Co-operative Bank Nagpur
HDFC0CNCCBL|NCCX|urban_coop|Nabadwip Co-operative Credit Bank
HDFC0CNDGCB|NDGX|district_coop|Nashik District Girna Sahakari Bank
HDFC0CNEELA|TNKX|urban_coop|Neela Krishna Co-operative Urban Bank
HDFC0CNGUBL|NGNX|urban_coop|Nagina Urban Co-operative Bank
HDFC0CNHUCB|TNHX|urban_coop|Nakodar Hindu Urban Co-operative Bank
HDFC0CNJMVB|NJMX|district_coop|Nashik Zila Mahila Vikas Sahakari Bank
HDFC0CNPCCB|NPPX|urban_coop|Nabagram Peoples' Co-operative Credit Bank
HDFC0CNSBLD|NGSX|urban_coop|Nagarik Sahakari Bank Maryadit Durg
HDFC0CNSBLN|NABX|urban_coop|Nandani Sahakari Bank
HDFC0CNSBLR|NSRX|urban_coop|Nagrik Sahakari Bank
HDFC0CNSBMJ|NSJX|urban_coop|Nagarik Sahakari Bank Maryadit,jagdalpur
HDFC0CNSBMV|NBMX|urban_coop|Nagrik Sahakari Bank, Vidisha
HDFC0CNUB01|NCOX|urban_coop|Nagnath Urban Co-operative Bank Hingoli
HDFC0CNUCB2|NGUX|urban_coop|Nagaur Urban Co-operative Bank
HDFC0CNUCBH|NILX|urban_coop|Nilkanth Co-operative Bank
HDFC0CNUCBK|NVCX|urban_coop|Navanagara Urban Co-operative Bank
HDFC0CNUCBL|NUBX|urban_coop|New Urban Co-operative Bank Rampur
HDFC0CNUCBN|TNUX|urban_coop|Nandura Urban Co-operative Bank Nandura
HDFC0COJSBL|OSMX|urban_coop|Osmanabad Janata Sahakari Bank
HDFC0COMC01|OMCX|urban_coop|Ojhar Merchant's Co-operative Bank
HDFC0COMMCO|CMLX|urban_coop|Commercial Co-operative Bank
HDFC0CPATAN|PCBL|urban_coop|Patan Co-operative Bank
HDFC0CPCB01|PCBX|urban_coop|Parshwanath Co-operative Bank
HDFC0CPCSBL|PCTX|urban_coop|Pune Cantonment Sahakari Bank
HDFC0CPDCCB|PDCX|district_coop|Pune District Central Co-operative Bank
HDFC0CPDPBK|APSX|urban_coop|P. D. Patilsaheb Sahakari Bank
HDFC0CPIMCO|PMCX|urban_coop|Pimpalgaon Merchants Co-operative Bank
HDFC0CPMCBL|PROX|urban_coop|Progressive Mercantile Co-operative Bank
HDFC0CPNSBL|PNSX|urban_coop|Poornawadi Nagrik Sahakari Bank
HDFC0CPPCB1|PPCX|urban_coop|Pachora Peoples Co-operative Bank
HDFC0CPRCBL|PREX|urban_coop|Prerana Co-operative Bank
HDFC0CPSBLN|PRSX|urban_coop|Pravara Sahakari Bank
HDFC0CPUB04|PALX|urban_coop|Pali Urban Co-operative Bank
HDFC0CPUBLP|PTNX|urban_coop|Patan Urban Co-operative Bank Patan
HDFC0CPUCBL|PGRX|urban_coop|Progressive Urban Co-operative Bank
HDFC0CPUCOB|PUBX|urban_coop|People's Urban Co-operative Bank
HDFC0CQNB01|QNBX|foreign|Qatar National Bank
HDFC0CRANUJ|RNBX|urban_coop|Ranuj Nagrik Sahakari Bank
HDFC0CRCBLB|RJJX|urban_coop|Rajajinagar Co-operative Bank
HDFC0CRCZUC|CZUX|urban_coop|Churu Zila Urban Co-operative Bank
HDFC0CRLMUB|RHMX|urban_coop|Raj Laxmi Mahila Urban Co-operative Bank Jaipur
HDFC0CRLUCB|RLUX|urban_coop|Ranilaxmibai Urban Co-operative Bank
HDFC0CRMUCB|RAMX|urban_coop|Rajputana Mahila Urban Co-operative Bak
HDFC0CRNGCB|RCBX|urban_coop|Raniganj Co-operative Bank
HDFC0CRNSSB|RDNX|urban_coop|Col Rd Nikam Sainik Sahakari Bank
HDFC0CRPCBL|RANX|urban_coop|Rander Peoples Co-operative Bank
HDFC0CRSSBL|RSSX|urban_coop|Rajarshi Shahu Sahakari Bank
HDFC0CS1812|SMEX|urban_coop|Sumerpur Mercantile Urban Co-operative Bank
HDFC0CSACBL|ADCX|urban_coop|Shri Adinath Co-operative Bank
HDFC0CSAMAT|SDBX|urban_coop|Samata Co-operative Development Bank
HDFC0CSANBK|SMTX|urban_coop|Sanmitra Urban Co-operative Bank
HDFC0CSBCBL|SRCX|urban_coop|Shree Bharat Co-operative Bank
HDFC0CSBL02|SAHX|urban_coop|Sadhana Sahakari Bank Pune
HDFC0CSBLIN|SBLD|foreign|Sonali Bank
HDFC0CSCBL1|SACX|urban_coop|Sarvodaya Co-operative Bank Mumbai
HDFC0CSCBL2|SCIX|urban_coop|Social Co-operative Bank
HDFC0CSCNSB|SCNX|urban_coop|Shri Chhani Nagrik Sahakari Bank
HDFC0CSCOBL|SIDX|urban_coop|Siddhi Co-operative Bank
HDFC0CSDSBL|SSBX|urban_coop|Shivdaulat Sahakari Bank
HDFC0CSGUCB|SGUX|urban_coop|Sadguru Gahininath Urban Co-operative Bank Akluj
HDFC0CSHREE|SREX|urban_coop|Shree Co-operative Bank
HDFC0CSHSBL|SHSX|urban_coop|Sharad Sahakari Bank
HDFC0CSIDDH|SDHX|urban_coop|Solapur Siddheshwar Sahakari Bank
HDFC0CSINDC|SDCX|district_coop|Sindhudurg District Central Co-operative Bank
HDFC0CSLCBL|SLCX|urban_coop|Shree Laxmi Co-operative Bank
HDFC0CSMMB1|SMMX|urban_coop|Shree Mahalaxmi Mercantile Co-operative Bank
HDFC0CSMSSB|SEWX|urban_coop|Shri Mahila Sewa Sahakari Bank
HDFC0CSMUCB|SMUX|urban_coop|Shri Mahavir Urban Co-operative Bank
HDFC0CSNCBL|SNAX|urban_coop|Saraspur Nagarik Co-operative Bank
HDFC0CSNCBR|SUNB|urban_coop|Surat National Co-operative Bank
HDFC0CSNSB0|SKNX|urban_coop|Sankheda Nagarik Sahakari Bank
HDFC0CSNSBH|SIHX|urban_coop|Sihor Nagarik Sahakari Bank
HDFC0CSNSBL|SONX|urban_coop|Sonbhadra Nagar Sahakari Bank
HDFC0CSNSBM|SGSX|urban_coop|Sadguru Nagrik Sahakari Bank Maryadit
HDFC0CSPCBK|HPCX|urban_coop|Shree Parswanath Co-operative Bank
HDFC0CSSB01|SAIX|urban_coop|Sanmitra Sahakari Bank
HDFC0CSSBNK|SHEX|urban_coop|Shree Samarth Sahakari Bank Nashik
HDFC0CSSSBL|SSSX|urban_coop|Sant Sopankaka Sahakari Bank
HDFC0CSSSBN|SHUX|urban_coop|Shushruti Souharda Sahakara Bank Niyamita
HDFC0CSSUCB|SSLX|urban_coop|Solapur Social Urban Co-operative Bank
HDFC0CSTUCB|STRX|urban_coop|Sterling Urban Co-operative Bank
HDFC0CSUB01|SHBX|urban_coop|Shriram Urban Co-operative Bank
HDFC0CSUCBL|SUCX|urban_coop|Sangli Urban Co-operative Bank
HDFC0CSUCOB|SSDX|urban_coop|Suco Souharda Sahakari Bank
HDFC0CSUCUB|SCUX|urban_coop|Sudha Co-operative Urban Bank
HDFC0CSUDHA|SDUX|urban_coop|Sri Sudha Co-operative Bank
HDFC0CSUVRN|SUVX|urban_coop|Suvarnayug Sahakari Bank
HDFC0CSVCBL|VCCX|urban_coop|Shri Veershaiv Co-operative Bank
HDFC0CSVSBL|SAVX|urban_coop|Sardar Vallabhbhai Sahakari Bank
HDFC0CSWMUB|SWMX|urban_coop|Sawai Madhopur Urban Co-operative Bank
HDFC0CSWSBL|SWSX|urban_coop|Shree Warana Sahakari Bank
HDFC0CTANSB|AGSX|urban_coop|Agrasen Nagari Sahakari Bank
HDFC0CTBCBL|BHAX|urban_coop|Bhagyodaya Co-operative Bank
HDFC0CTBMCB|BMPX|urban_coop|Banaskantha Mercantile Co-operative Bank
HDFC0CTBNSB|BAVX|urban_coop|Bavla Nagrik Sahakari Bank
HDFC0CTBUCB|BORX|urban_coop|Boral Union Co-operative Bank
HDFC0CTCCBK|TCCX|urban_coop|Commercial Co-operative Bank
HDFC0CTGCUB|GCUX|urban_coop|Gayatri Co-operative Urban Bank
HDFC0CTGNSB|GNSX|urban_coop|Gozaria Nagrik Sahakari Bank
HDFC0CTGUCB|TGUX|urban_coop|Gandhinagar Urban Co-operative Bank
HDFC0CTJCBL|JANX|urban_coop|Janata Co-operative Bank
HDFC0CTJPCB|JMPX|urban_coop|Jamnagar Peoples Co-operative Bank
HDFC0CTKNSB|KUKX|urban_coop|Kukarwada Nagarik Sahakari Bank
HDFC0CTMCBL|MVCX|urban_coop|Mahaveer Co-operative Bank
HDFC0CTMCNB|MNBX|urban_coop|Mahila Co-operative Nagarik Bank,bharuch
HDFC0CTPHCB|PHIX|urban_coop|Panihati Co-operative Bank
HDFC0CTR384|TUBX|urban_coop|Trichur Urban Co-operative Bank
HDFC0CTRUMC|RUMX|urban_coop|Raipur Urban Mercantile Co-operative Bank
HDFC0CTSCBL|SNGX|urban_coop|Sarangpur Co-operative Bank
HDFC0CTSGCB|TSCX|urban_coop|Santragachi Co-operative Bank
HDFC0CTSMCB|SMCX|urban_coop|Sangamner Merchants Co-operative Bank
HDFC0CTTCBL|TETX|urban_coop|Textile Traders Co-operative Bank
HDFC0CTUB02|TUCX|urban_coop|Tirupati Urban Co-operative Bank
HDFC0CTUCB1|UTBX|urban_coop|Uttarpara Co-operative Bank
HDFC0CTUCBL|FGCB|urban_coop|Fingrowth Co-operative Bank
HDFC0CTUCOB|TAPX|urban_coop|Tapindu Urban Co-operative Bank
HDFC0CTZNBK|CCBX|urban_coop|Citizen Co-operative Bank
HDFC0CUCBLM|TUCL|urban_coop|The Union Co-operative Bank Mahinagar
HDFC0CUCBNR|TUNX|urban_coop|Union Co-operative Bank
HDFC0CUCOBL|UTCX|urban_coop|Uttrakhand Co-operative Bank
HDFC0CUCUB9|UCUX|urban_coop|Universal Co-operative Urban Bank
HDFC0CUPCBA|UNAX|urban_coop|Una Peoples Co-operative Bank
HDFC0CUUCB2|UMCX|urban_coop|Umreth Urban Co-operative Bank
HDFC0CVB003|VMCX|urban_coop|Vardhaman (mahila) Co-operative Urban Bank
HDFC0CVCBL1|VDYX|urban_coop|Vidyanand Co-operative Bank
HDFC0CVCBLN|VCBX|urban_coop|Vishwas Co-operative Bank
HDFC0CVMCBA|VERX|urban_coop|Veraval Mercantile Co-operative Bank
HDFC0CVPCBL|TVPX|urban_coop|Veraval Peoples Co-operative Bank
HDFC0CVSBHO|VSSX|urban_coop|Veerashaiva Sahakari Bank
HDFC0CVSBLS|VKSX|urban_coop|Vikas Sahakari Bank Solapur
HDFC0CVSBMS|VYPX|urban_coop|Vyapari Sahakari Bank Maryadit Solapur
HDFC0CVUCBL|VUCX|urban_coop|Vaidyanath Urban Co-operative Bank
HDFC0CVVCCB|VVCX|urban_coop|Vallabh Vidyanagar Commercial Bank
HDFC0CWOMEN|WCBX|urban_coop|Women's Co-operative Bank
HDFC0CWUCBL|WUCX|urban_coop|Washim Urban Co-operative Bank.
HDFC0CYACBP|YCBX|urban_coop|Yashwant Co-operative Bank
HDFC0CYMSB1|YMSX|urban_coop|Yavatmal Mahila Sahakari Bank
HDFC0CYNSBL|YNSX|urban_coop|Yeshwant Nagari Sahakari Bank
IBKL0001NSB|NSIX|urban_coop|Nagrik Sahakari Bank Indore
IBKL0008BCB|TBCX|urban_coop|Bharath Co-operative Bank
IBKL0015NSB|NSBX|urban_coop|Nagrik Sahakari Bank Lucknow
IBKL0031MCB|MUBX|urban_coop|Maharaja Co-operative Urban Bank
IBKL0031VCB|VISX|urban_coop|Visakhapatnam Co-operative Bank
IBKL0041SCB|SCOB|urban_coop|Samruddhi Co-operative Bank
IBKL0041SSB|SSKX|urban_coop|Sadhana Sahakari Bank
IBKL0041Y01|YAVX|urban_coop|Yavatmal Urban Co-operative Bank
IBKL0046T01|TNDC|district_coop|Thiruvananthapuram District Co-operative Bank
IBKL0063JCB|JSCX|state_coop|Jharkhand State Co-operative Bank
IBKL0065SDC|STDX|district_coop|Samastipur District Central Co-operative Bank
IBKL0068GP1|GPCX|urban_coop|Gandevi People's Co-operative Bank
IBKL0069AZB|AZUX|urban_coop|Azad Urban Co-operative Bank Hubli
IBKL0069S01|SUSX|urban_coop|Sirsi Urban Sahakari Bank
IBKL0069SMS|SMSX|urban_coop|Soubhagya Mahila Souhardha Sahakari Bank
IBKL0078MCC|TMBX|urban_coop|Mangalore Catholic Co-operative Bank
IBKL0078MCT|TMTX|urban_coop|Mangalore Co-operative Town Bank
IBKL0080RUC|RSUX|urban_coop|Radhasoami Urban Co-operative Bank
IBKL0086JMC|TJMX|urban_coop|Jaynagar Mozilpur Peoples Co-operative Bank
IBKL0087PCS|PCPX|urban_coop|Pimpri Chinchwad Sahakari Bank Maryadit,pimpri
IBKL0087PSB|PVAX|urban_coop|Pavana Sahakari Bank
IBKL0100ACB|ACCX|urban_coop|Ace Co-operative Bank
IBKL0100ITB|ITDX|urban_coop|Income Tax Dept Co-operative Bank
IBKL0100NS2|NSBB|urban_coop|Nagarik Sahakari Bank Bhiwandi
IBKL0100SKP|KNNX|urban_coop|Sri Kannikaparameswari Co-operative Bank
IBKL01011GC|GGCX|district_coop|Gopalganj Central Co-operative Bank
IBKL0101BBU|BBUX|urban_coop|Bellad Bagewadi Urban Souharada Sahakari Bank Nyt
IBKL0101BDC|BELX|district_coop|Belgaum District Central Co-operative Bank
IBKL0101BZR|ZRNB|urban_coop|Rani Channamma Mahila Sahakari Bank
IBKL0101MCB|MRTX|urban_coop|Maratha Co-operative Bank
IBKL0101SBC|BLGX|urban_coop|Shree Basaveshwar Co-operative Bank
IBKL0103925|BDBX|district_coop|Bellary District Co-operative Central Bank
IBKL01064NS|NGRX|urban_coop|Nagar Sahakari Bank
IBKL01066RC|ROCX|district_coop|Rohika Central Co-operative Bank Madhubani
IBKL01071BD|BODX|district_coop|Bagalkot District Central Co-operative Bank
IBKL01071BP|BPSX|urban_coop|Bilagi Pattana Sahakari Bank Niyamit
IBKL01075MC|TMCX|district_coop|Motihari Central Co-operative Bank
IBKL01076SB|SIWX|district_coop|Siwan Central Co-operative Bank
IBKL01077BD|BGUX|district_coop|Begusarai Central Co-operative Bank
IBKL01077KD|KGDX|district_coop|Khagaria District Central Co-operative Bank
IBKL01078MJ|MJCX|district_coop|Monghyr Jamui Central Co-operative Bank
IBKL0108SIC|SKKX|state_coop|Sikkim State Co-operative Bank
IBKL01095NU|NLUX|urban_coop|National Urban Co-operative Bank,bahraich
IBKL0114BCU|TBUX|urban_coop|Balusseri Co-operative Urban Bank
IBKL0114K01|KDCX|district_coop|Kozhikode District Co-operative Bank
IBKL0114KMB|KEMX|urban_coop|Kerala Mercantile Co-operative Bank
IBKL0114NUB|NADX|urban_coop|Nadapuram Co-operative Bank
IBKL0114PCB|PYCX|urban_coop|Payyoli Co-operative Urban Bank
IBKL0115GCU|GCUL|urban_coop|Gauhati Co-operative Urban Bank
IBKL0115ICB|ICBL|urban_coop|Industrial Co-operative Bank
IBKL0116APC|ASHX|urban_coop|Ashta People's Co-operative Bank
IBKL0116AUC|AUCX|urban_coop|Ajara Urban Co-operative Bank
IBKL0116DPC|DOBX|urban_coop|Dapoli Urban Co-operative Bank
IBKL0116HSB|HUTX|urban_coop|Hutatma Sahakari Bank
IBKL0116JSB|JSAX|urban_coop|Janata Sahakari Bank Ajara
IBKL0116KCB|KAGX|urban_coop|Kagal Co-operative Bank Kagal
IBKL0116KUC|KULX|urban_coop|Kodoli Urban Co-operative Bank Kodoli
IBKL0116MCO|MHMX|urban_coop|Shri Mahalaxmi Co-operative Bank Kolhapur
IBKL0116RBS|RBBX|urban_coop|Rajarambapu Sahakari Bank Peth
IBKL0116RSB|REBX|urban_coop|Rendal Sahakari Bank Rendal
IBKL0116SBK|SKUX|urban_coop|S S L S A Kurundwad Urban Bank
IBKL0116SSB|SPTX|urban_coop|Shripatraodada Sahakari Bank
IBKL0116VMC|VCOX|urban_coop|Vita Merchants Co-operative Bank
IBKL01192AC|AURX|district_coop|Aurangabad District Central Co-operative Bank. Bihar
IBKL0123NMC|NMCX|urban_coop|Navi Mumbai Co-operative Bank
IBKL01241MB|MSSX|urban_coop|Merchants Souharda Sahakara Bank Niyamitha
IBKL01248NC|TNCX|district_coop|National Central Co-operative Bank Bettiah
IBKL01263UC|URDX|urban_coop|Urban Co-operative Bank Budaun
IBKL01269UB|URMX|urban_coop|Urban Co-operative Bank Mainpuri
IBKL01272PC|PRCX|urban_coop|Proddatur Co-operative Town Bank
IBKL01282KB|KNBX|urban_coop|Kalol Nagarik Sahakari Bank
IBKL01307KU|KUTX|urban_coop|Kuttiady Co-operative Urban Bank
IBKL01340DT|SDTC|urban_coop|Shri D T Patil Co-operative Bank
IBKL01340JC|TJBX|urban_coop|Janata Co-operative Bank Sadalga
IBKL01365B1|BAUX|urban_coop|Badagara Co-operative Urban Bank
IBKL0140PCC|TPCX|urban_coop|Patliputra Central Co-operative Bank
IBKL0150KMC|KBCX|urban_coop|Kanakamahalakshmi Co-operative Bank
IBKL01529GM|GMUX|urban_coop|Gulshan Mercantile Urban Co-operative Bank
IBKL0158SCU|TSIX|urban_coop|Shillong Co-operative Urban Bank
IBKL01600SP|SPCX|urban_coop|Shirpur Peoples Co-operative Bank
IBKL01606BM|BOTX|urban_coop|Shree Botad Mercantile Co-operative Bank
IBKL01606BP|TBPX|urban_coop|Botad Peoples Co-operative Bank
IBKL01642C1|CTBX|urban_coop|Citizens Co-operative Bank
IBKL01642RP|RAKX|urban_coop|Rajkot Peoples Co-operative Bank
IBKL01642SD|SRHX|urban_coop|Shree Dharati Co-operative Bank
IBKL01720GU|TGCG|urban_coop|Gokak Urban Co-operative Credit Bank
IBKL0186LEC|LECX|urban_coop|LIC Employees Co-operative Bank
IBKL01870N1|NULX|urban_coop|National Urban Co-operative Bank, Pratapgarh
IBKL0209M01|MPDX|district_coop|Malappuram District Co-operative Bank
IBKL0218HNS|HMNX|urban_coop|Himatnagar Nagarik Sahakari Bank
IBKL0232UCB|UCBX|urban_coop|Urban Co-operative Bank Bareilly
IBKL0236UCS|TUOX|urban_coop|Urban Co-operative Bank Saharanpur
IBKL0250A01|AMSB|urban_coop|Annasaheb Magar Sahakari Bank
IBKL0256DSC|DSPX|urban_coop|Durgapur Steel Peoples' Co-operative Bank
IBKL0269KTC|KODX|urban_coop|Kodungallur Town Co-operative Bank
IBKL0269TDC|TDCX|district_coop|Thrissur District Co-operative Bank
IBKL0280KCB|TKCX|urban_coop|Kakatiya Co-operative Urban Bank
IBKL0294MCC|MFCX|district_coop|Muzaffarpur Central Co-operative Bank
IBKL0296RDC|THRX|district_coop|Raichur District Central Co-operative Bank
IBKL0297TSM|SEMX|urban_coop|Secunderabad Mercantile Co-operative Urban Bank
IBKL0299RDC|RDCX|district_coop|Raigad District Central Co-operative Bank
IBKL0339PDC|PATX|district_coop|Pathanmthitta District Co-operative Bank
IBKL0340KU1|KCUX|urban_coop|Kannur Co-operative Urban Bank
IBKL0340NCU|NIUX|urban_coop|Nileshwar Co-operative Urban Bank
IBKL0340PC1|PAYX|urban_coop|Payangadi Urban Co-operative Bank
IBKL0364BCB|BCOX|urban_coop|Bapuji Co-operative Bank
IBKL0365SVC|SVAX|urban_coop|Sri Vasavamba Co-operative Bank
IBKL0401THC|THCX|urban_coop|Hooghly Co-operative Credit Bank
IBKL0413KSB|KESX|urban_coop|Keshav Sehkari Bank
IBKL0413RCB|RGCX|urban_coop|Ramgarhia Co-operative Bank
IBKL0414MCB|MGDX|district_coop|Magadh Central Co-operative Bank
IBKL0420MPC|TMPX|urban_coop|Midnapore Peoples Co-operative Bank
IBKL0427NCB|NAWX|urban_coop|Nawanagar Co-operative Bank
IBKL0443MCB|COMX|urban_coop|Co-operative Bank of Mehsana
IBKL0443MNB|MNSX|urban_coop|Mehsana Nagarik Sahakari Bank
IBKL0443SCC|SVCX|urban_coop|Sarvodaya Commerical Co-operative Bank
IBKL0450KCT|KSTX|urban_coop|Kasaragod Co-operative Town Bank No 970
IBKL0450TKD|KADX|district_coop|Kasaragod District Co-operative Bank
IBKL0452JSB|JSAB|urban_coop|Jain Sahakari Bank
IBKL0452MCB|MGCB|urban_coop|Mogaveera Co-operative Bank
IBKL0452ND1|NDCB|urban_coop|Naval Dockyard Co-operative Bank
IBKL0452VK1|VKCX|urban_coop|Vima Kamgar Co-operative Bank
IBKL0458NJM|NJSX|district_coop|Nasik Zila Mahila Sahakari Bank
IBKL0459SBS|SMBX|urban_coop|Sampada Sahakari Bank
IBKL0463DYP|DYPX|urban_coop|D.y.patil Sahakari Bank Kolhapur
IBKL0463KDC|KPCX|district_coop|Kolhapur District Central Co-operative Bank
IBKL0463KS1|KKSX|urban_coop|Kumbhi Kasari Bank Kuditre
IBKL0464GSB|RSVX|urban_coop|Rajarshi Shahu Govt Servants Co-operative Bank Kolh
IBKL0464PNS|SPNX|urban_coop|Shree Panchganga Nagari Sahakari Bank
IBKL0464PSS|PSSX|urban_coop|Prathamik Shikshak Sahakari Bank
IBKL0467JSB|JIVX|urban_coop|Jivaji Sahakari Bank  Ichalkaranji
IBKL0467NS1|NNSX|urban_coop|Nutan Nagari Sahakari Bank Ichalkaranji
IBKL046KS01|KSCX|state_coop|Kerala State Co-operative Bank
IBKL0470KRS|KSBX|urban_coop|Krishna Sahakari Bank Rethare Bk
IBKL0478ADC|AHUX|urban_coop|Ahilyadevi Urban Co-operative Bank Solapur
IBKL0478KCB|KAMX|urban_coop|Kamala Co-operative Bank Solapur
IBKL0478LOK|LKMX|urban_coop|Lokmangal Co-operative Bank Solapur
IBKL0478MCB|MASX|urban_coop|Mahesh Urban Co-operative Bank Solapur
IBKL0478SNB|SRSX|urban_coop|Sharad Nagari Sahakari Bank
IBKL0483DND|DNDC|district_coop|Dhule And Nandurbar District Central Co-operative Bank
IBKL0485SDC|SDSX|district_coop|Satara District Central Co-operative Bank
IBKL0487SDC|SGCX|district_coop|Sangli District Central Co-operative Bank
IBKL0497LDC|LDCX|district_coop|Latur District Central Co-operative Bank
IBKL0497LUC|LATX|urban_coop|Latur Urban Co-operative Bank Latur
IBKL0500NMC|TNMX|urban_coop|Nanded Merchants Co-operative Bank Nanded
IBKL0501DMC|DMCB|urban_coop|Deccan Merchants Co-operative Bank
IBKL0501VSB|VAIX|urban_coop|Vaishya Sahakari Bank Mumbai
IBKL0510WUC|WAUX|urban_coop|Wardhaman Urban Co-operative Bank Nagpur
IBKL0530JDC|JLDX|district_coop|Jalna District Central Co-operative Bank
IBKL0548PMC|PNMX|urban_coop|Pune Merchant's Co-operative Bank
IBKL0548PPC|PPBX|urban_coop|Pune People's Co-operative Bank
IBKL0551GMC|GRAX|urban_coop|Grain Merchants' Co-operative Bank
IBKL0553MSC|MSBX|urban_coop|M.s.Co-operative Bank
IBKL0553TBT|TBTX|urban_coop|Baroda Traders Co-operative Bank
IBKL0553TWU|WACX|urban_coop|Waghodia Urban Co-operative Bank
IBKL0574RDC|RNDX|district_coop|Ratnagiri District Central Co-operative Bank
IBKL0579BSB|BVSX|urban_coop|Bhavani Sahakari Bank
IBKL0596BDC|BDIX|district_coop|Bhandara District Central Co-operative Bank
IBKL0608TUC|TTGX|urban_coop|Tasgaon Urban Co-operative Banktasgaon
IBKL0617STB|STCX|urban_coop|State Transport Co-operative Bank
IBKL0693TCB|TCHX|urban_coop|Town Co-operative Bank Hoskote
IBKL0697ANS|ANBX|urban_coop|Amreli Nagarik Sahakari Bank
IBKL070CZSB|CHAX|district_coop|Chamoli Zila d Bank
IBKL070TGZS|TEHX|district_coop|Tehri Garhwal Zila Sahakari Bank
IBKL0719SCB|SITX|district_coop|Sitamarhi Central Co-operative Bank
IBKL0721BCC|BHGX|district_coop|Bhagalpur Central Co-operative Bank
IBKL0722CCB|CRBX|district_coop|Central Co-operative Bank Ara
IBKL0724VDC|VHDX|district_coop|Vaishali District Central Co-operative Bank
IBKL0729SUC|SNCX|urban_coop|Sonepat Urban Co-operative Bank
IBKL0737Q01|QUCX|urban_coop|Quilon Co-operative Urban Bank
IBKL0743KMC|KOCX|urban_coop|Konoklota Mahila Urban Co-operative Bank
IBKL0752BCB|BGCX|district_coop|Balageria Central Co-operative Bank
IBKL0763CCB|CHRX|urban_coop|Cherpalcheri Co-operative Urban Bank
IBKL0763OCB|TOCX|urban_coop|Ottapalam Co-operative Urban Bank
IBKL0763P10|TPUX|urban_coop|Urban Co-operative Bank No 1758 Perinthalmanna
IBKL0763SCU|TSNX|urban_coop|Shoranur Co-operative Urban Bank
IBKL0768PJS|PITX|district_coop|Pithoragarh Zila Sahakari Bank
IBKL0773BCB|BCUX|urban_coop|Baghat Urban Co-operative Bank
IBKL0776SPS|SPSX|urban_coop|Sandur Pattana Souharda Sahakari Bank Niyamitha
IBKL0780NCB|NACX|district_coop|Nalanda Central Co-operative Bank Nalanda
IBKL0782SBC|TSBX|district_coop|Sasaram Bhabhua Central Co-operative Bank
IBKL078SCDC|SCDX|district_coop|South Canara District Central Co-operative Bank
ICIC00ABWCC|ARMX|urban_coop|510 Army Base W/s Credit Co-operative Primary Bank
ICIC00ADCCB|AHMX|district_coop|Ahmednagar District Central Co-operative Bank
ICIC00ADRSH|ACUX|urban_coop|Adarsh Co-operative Urban Bank
ICIC00AGDCB|AGDX|district_coop|Agra District Co-operative Bank
ICIC00AJHCB|AJSX|urban_coop|Ambarnath Jai-hind Co-operative Bank
ICIC00ALDCB|ALLX|district_coop|Allahabad District Co-operative Bank
ICIC00AMSBL|AMAX|urban_coop|Aman Sahakari Bank
ICIC00ARIHT|SACB|urban_coop|Shri Arihant Co-operative Bank
ICIC00ASHSB|ASSX|urban_coop|Ashok Sahakari Bank
ICIC00AZSBL|ALIX|district_coop|Aligarh District Co-operative Bank
ICIC00BAHDC|BDBB|district_coop|Bahraich District Co-operative Bank
ICIC00BALUC|BTUX|urban_coop|Balotra Urban Co-operative Bank
ICIC00BASTI|UBBX|urban_coop|Urban Co-operative Bank Basti
ICIC00BBDCB|DIBX|district_coop|District Co-operative Bank Barabanki
ICIC00BDCBL|BDOX|district_coop|Banda District Co-operative Bank
ICIC00BIJNR|ZSBX|district_coop|Zila Sahakari Bank Bijnor
ICIC00BMCBL|BANX|urban_coop|Banaras Mercantile Co-operative Bank
ICIC00BULND|ZBBX|district_coop|Zila Sahakari Bank Bulandshahar
ICIC00CCBLT|CONX|urban_coop|Contai Co-operative Bank
ICIC00CNSBL|CNSX|urban_coop|Chembur Nagarik Sahakari Bank
ICIC00COMCB|CMCX|urban_coop|Commercial Co-operative Bank
ICIC00DCBLS|DSHX|district_coop|District Co-operative Bank Saharanpur
ICIC00DCBSU|DSAX|district_coop|District Central Co-operative Bank, Supaul
ICIC00DCCBG|ZSBG|district_coop|Zila Sahakari Bank Gorakhpur
ICIC00DKDCB|DKCL|district_coop|Deoria Kasia District Co-operative Bank
ICIC00DUCBL|DEUX|urban_coop|Devika Urban Co-operative Bank
ICIC00ETAWH|ETCX|district_coop|Etawah District Co-operative Bank Etwah
ICIC00ETDCB|ETDX|district_coop|Etah District Co-operative Bank
ICIC00ETUCB|EUCX|urban_coop|Etah Urban Co-operative Bank
ICIC00FDCBL|FDFX|district_coop|Farrukhabad District Co-operative Bank Fatehgarh
ICIC00FDCCB|FCOX|district_coop|District Co-operative Bank Faizabad
ICIC00FZSBL|FZSX|district_coop|Firozabad Zila d Bank
ICIC00GUCCB|GADX|urban_coop|Gadhinglaj Urban Co-operative Bank
ICIC00HDCBL|HDCL|district_coop|Hardoi District Co-operative Bank
ICIC00HDCCB|HAMX|district_coop|Hamirpur District Co-operative Bank
ICIC00HHSBL|HSBX|urban_coop|Harihareshwar Sahakari Bank
ICIC00INPRS|IPSX|urban_coop|Indore Paraspar Sahakari Bank
ICIC00JCBLG|JCPX|urban_coop|Janata Co-operative Bank
ICIC00JDCBL|JDCX|district_coop|Jalaun District Co-operative Bank
ICIC00JDCCB|TJDX|district_coop|Jalgaon District Central Co-operative Bank Jalgaon
ICIC00JUSBL|JUSX|urban_coop|Jaysingpur Udgaon Sahakari Bank Jaysingpur
ICIC00KDCBL|KHCX|urban_coop|Khardah Co-operative Bank
ICIC00KHIRI|ZSLX|district_coop|Zila Sahakari Bank Lakhimpur Kheri
ICIC00KURLA|KNSB|urban_coop|Kurla Nagarik Sahakari Bank
ICIC00KUTCB|KCBX|urban_coop|Kutch Co-operative Bank
ICIC00KZSBL|ZSKX|district_coop|Zila Sahakari Bank Kanpur
ICIC00LDCBL|ZLLX|district_coop|Zila Sahakari Bank Lalitpur
ICIC00LMNSB|LBMX|urban_coop|Laxmibai Mahila Nagrik Sahakari Bank Maradit
ICIC00LUCBL|LULX|urban_coop|Laxmi Urban Co-operative Bank Latur
ICIC00MAUCB|MHUX|urban_coop|Mahesh Urbank Co-operative Bank Parli V.
ICIC00MCUBL|MAJX|urban_coop|Manjeri Co-operative Urban Bank
ICIC00MDCCB|DCMX|district_coop|District Co-operative Bank Mainpuri
ICIC00MDPPC|MDPX|urban_coop|Md Pawar Peoples Co-operative Bank Urun Islampur
ICIC00MERUT|ZSMX|district_coop|Zila Sahakari Bank Meerut
ICIC00MIRZA|ZIMX|district_coop|Zila Sahakari Bank Mirzapur
ICIC00MMCBL|MBLX|urban_coop|Manmandir Co-operative Bank
ICIC00MORAD|ZMMX|district_coop|Zila Sahakari Bank Moradabad
ICIC00MPCUB|MAPX|urban_coop|Maharana Pratap Co-operative Urban Bank
ICIC00MUCBA|MCAX|urban_coop|Mahesh Urban Co-operative Bank Ahmedpur
ICIC00MUPCB|MUPX|urban_coop|Mehmadabad Urban Peoples Co-operative Bank
ICIC00MUZAF|MCDX|district_coop|Muzaffarnagar District Co-operative Bank
ICIC00MZSBL|ZBSX|district_coop|Zila Sahakari Bank Mathura
ICIC00NDCCB|NASX|district_coop|Nasik District Central Co-operative Bank
ICIC00NDIMC|NDIX|urban_coop|Nashik District Industrial & Mercantile Co-operative Bank
ICIC00NGSBL|NSPX|urban_coop|Nishigandha Sahakari Bank
ICIC00NIDHI|NIDX|urban_coop|Nidhi Co-operative Bank
ICIC00NVJVN|NJCX|urban_coop|Nav Jeevan Co-operative Bank
ICIC00PDCBL|DCPX|district_coop|District Co-operative Bank Pilibhit
ICIC00PRDCB|DTPX|district_coop|District Co-operative Bank Pratapgarh
ICIC00PURVA|PVCX|urban_coop|Purvanchal Co-operative Bank Gazipur
ICIC00RAMPR|RZSX|district_coop|Rampur Zila Sahakari Bank
ICIC00RBDCB|DTCX|district_coop|District Co-operative Bank Rae Bareli
ICIC00RCCBL|RACX|urban_coop|Rajkot Commercial Co-operative Bank
ICIC00RNSBL|RNSX|urban_coop|Rajdhani Nagar Sahakari Bank
ICIC00RUCBL|RPUX|urban_coop|Rajapur Urban Co-operative Bank
ICIC00SBCUB|SBCX|urban_coop|Sultan's Bathery Co-operative Urban Bank
ICIC00SBSBN|BASX|urban_coop|Shri Basaveshwar Sahakari Bank Nyt.bagalkot
ICIC00SBUCB|SBUX|urban_coop|Shree Balaji Urban Co-operative Bank
ICIC00SEVUC|SEUX|urban_coop|Sevalia Urban Co-operative Bank
ICIC00SHNSB|SVNX|urban_coop|Shivaji Nagari Sahakari Bank
ICIC00SIDRH|URCX|urban_coop|Urban Co-operative Bank Siddharthnagar
ICIC00SIDUC|SURX|urban_coop|Siddheshwar Urban Co-operative Bank Maryadit Sillod
ICIC00SJDCB|DCSX|district_coop|District Co-operative Bank Shahjahanpur
ICIC00SMRTH|SSOX|urban_coop|Samarth Sahakari Bank
ICIC00SMSBL|SAPX|urban_coop|Shrimant Malojiraje Sahakari Bank
ICIC00SULCB|SULX|urban_coop|Sulaimani Co-operative Bank
ICIC00TGMUC|GAMX|urban_coop|Ganga Mercantile Urban Co-operative Bank
ICIC00TMUCB|MAYX|urban_coop|Mayani Urban Co-operative Bank
ICIC00TSCBL|TPSX|state_coop|Tripura State Co-operative Bank
ICIC00TUCBL|TTUX|urban_coop|Tirur Urban Co-operative Bank
ICIC00TVCBL|VRDX|urban_coop|Vardhaman Co-operative Bank
ICIC00TVUCB|TVUX|urban_coop|Vita Urban Co-operative Bank
ICIC00UDCCB|ZILX|district_coop|Zila Sahakari Bank Unnao
ICIC00UMUCB|TUMX|urban_coop|Udaipur Mahila Urban Co-operative Bank
ICIC00USCBD|UTKX|state_coop|Uttarakhand State Co-operative Bank
ICIC00USNDC|USNX|district_coop|Udham Singh Nagar District Co-operative Bank
ICIC00VDCBL|DCTX|district_coop|District Co-operative Bank Teliyabagh
ICIC00VNSBL|VNSX|urban_coop|Vivekanand Nagrik Sahakari Bank Mydt
ICIC00VSCBL|VSCX|urban_coop|Vikas Souharda Co-operative Bank
ICIC00ZSBBU|BUZX|district_coop|Badaun Zila d Bank
ICIC00ZSBHA|ZSHX|district_coop|Zila Sahakari Bank Haridwar
ICIC00ZSBLB|ZIBX|district_coop|Zila Sahakari Bank Bareilly
ICIC00ZSBLJ|ZSJX|district_coop|Zila Sahakari Bank Jhansi
ICIC00ZSBLS|DISX|district_coop|District Co-operative Bank, Sitapur
ICIC00ZSKTW|ZSGX|district_coop|Zila Sahakari Bank Garhwal Kotdwar
ICIC00ZSMAU|ZSAX|district_coop|Zila Sahakari Bank Mau
IDIB0PBG001|PBGX|rrb|Puduvai Bharathiar Grama Bank
IDIB0PLB001|PABX|rrb|Pallavan Grama Bank
IDIB0SGB001|SPBX|rrb|Saptagiri Grameena Bank
INDB0BCCB02|BCCB|urban_coop|Bangalore City Co-operative Bank
INDB0KUCB01|KATX|urban_coop|Kattappana Urban Co-operative Bank
INDB0NCCB00|NOIX|urban_coop|Noida Commercial Co-operative Bak
INDB0NNCB01|TNEX|urban_coop|Nehrunagar Co-operative Bank
INDB0NSBG01|NGKX|urban_coop|Nagrik Sahakari Bank Maryadit Gwalior
INDB0RBECBL|RBCX|urban_coop|Reserve Bank Employees Co-operative Bank
INDB0SBMC01|SBMX|urban_coop|Sri Banashankari Mahila Co-operative Bank
INDB0SCSC02|SCSX|urban_coop|Sree Charan Souhardha Co-operative Bank
INDB0SSBN01|SSNX|urban_coop|Sreenidhi Souharda Sahakari Bank Niyamitha
INDB0STCB01|TYCX|urban_coop|Shree Thyagaraja Co-operative Bank
INDB0TMCB01|TEMX|urban_coop|Textile Manufacturers Co-operative Bank
INDB0VCB001|SMVC|urban_coop|Sir M Visvesvaraya Co-operative Bank
IOBA0ROGB01|ODGB|rrb|Odisha Gramya Bank
JAKA0GRAMEN|XJKG|rrb|J&K Grameen Bank
KKBK0ACUB01|AGRX|urban_coop|Agroha Co-operative Urban Bank
KKBK0BCCB04|BARX|urban_coop|Baroda City Co-operative Bank
KKBK0BUCB01|BBLX|urban_coop|Bhingar Urban Co-operative Bank
KKBK0DCUB01|DCNX|urban_coop|Deccan Co-operative Urban Bank
KKBK0JCUB01|JCUX|urban_coop|Jagruti Co-operative Urban Bank
KKBK0KCUB01|KRMX|urban_coop|Karamana Co-operative Urban Bank
KKBK0KMCB02|KMCB|urban_coop|Kokan Mercantile Co-operative Bank
KKBK0MCUB01|TMNX|urban_coop|Mahanagar Co-operative Urban Bank
KKBK0MMCB01|MMCX|urban_coop|Mattancherry Mahajanik Co-operative Urban Bank
KKBK0MNSB01|TMSX|urban_coop|Modasa Nagarik Sahakari Bank
KKBK0MSCB01|MCSX|urban_coop|Mattancherry Sarvajanik Co-operative Bank
KKBK0MSCMB1|TMSC|urban_coop|Mysore Silk Cloth Merchants Co-operative Bank
KKBK0NCUB01|NUCX|urban_coop|Navabharat Co-operative Urban Bank
KKBK0NTCUB1|NEYX|urban_coop|Neyyattinkara Co-operative Urban Bank
KKBK0NZSPK1|NPKX|urban_coop|Nashik Zilha Sarkari & Parishad Karmachari Sb Nmt
KKBK0PCBL01|PGCX|urban_coop|Progressive Co-operative Bank
KKBK0PNSB01|PDSX|urban_coop|Priyadarshani Nagari Sahakari Bank Jalna.
KKBK0RCUB01|RCUX|urban_coop|Rajadhani Co-operative Urban Bank
KKBK0RRUB01|RNGX|urban_coop|Ranga Reddy Co-operative Urban Bank
KKBK0SMSB01|MAVX|urban_coop|Shree Mahavir Sahakari Bank
KKBK0SRSB01|RUKX|urban_coop|Shri Rukmini Sahakari Bank
KKBK0SSBL02|SABX|urban_coop|Saraswati Sahakari Bank
KKBK0TCSB02|CHTX|urban_coop|Chitnavispura Sahakari Bank
KKBK0TKCB01|KRNX|urban_coop|Karnavati Co-operative Bank
KKBK0TNCB02|NCBL|urban_coop|National Co-operative Bank
KKBK0UCB001|TUDX|urban_coop|Urban Co-operative Bank Dharangaon
KKBK0VCCB01|VICX|urban_coop|Vijay Commercial Co-operative Bank
KKBK0VUCB01|VAUX|urban_coop|Valmiki Urban Co-operative Bank
KSCB0002001|BBRX|district_coop|Bangalore  Bangalore Rural&ramanagara Dccb
KSCB0003001|KRCX|district_coop|Kolar And Chickballapur Dt Co-operative Central Bank
KSCB0004001|CITX|district_coop|Chitradurga District Co-operative Central Bank
KSCB0005001|SCOX|district_coop|Shimoga District Co-operative Central Bank
KSCB0007001|MYSX|district_coop|Mysore Chamarajanagar District Co-operative Bank
KSCB0008001|MDIX|district_coop|Mandya District Co-operative Central Bank
KSCB0011001|KDUX|district_coop|Kodagu District Co-operative Central Bank
KSCB0012001|CIDX|district_coop|Chikmagalur District Central Co-operative Bank
KSCB0014001|BDCX|district_coop|Bijapur District Central Co-operative Bank
KSCB0015001|KCDX|district_coop|Karnatak Central Co-operative Bank Dharwad
KSCB0016001|KANX|district_coop|Kanara District Central Co-operative Bank
KSCB0017001|KYDX|district_coop|Kalaburagi and Yadgir District Co-operative Central Bank
KSCB0018001|TDBX|district_coop|District Co-operative Central Bank
KSCB0701001|DVDX|district_coop|Davanagere District Central Co-operative Bank
ORCB0ANG001|AUBX|district_coop|Angul United Central Co-operative Bank
ORCB0ASK001|TSKX|urban_coop|Aska Co-operative Central Bank
ORCB0BLR001|BAGX|urban_coop|Balangir District Central Co-operative Bank
ORCB0BLS001|BBSX|district_coop|Balasore Bhadrak Central Co-operative Bank
ORCB0BOU001|BOCX|urban_coop|Boudh Co-operative Central Bank
ORCB0BWP001|BAWX|urban_coop|Bhawanipatna Central Co-operative Bank
ORCB0CTC001|CUTX|urban_coop|Cuttack Central Co-operative Bank
ORCB0KHU001|KHTX|urban_coop|Khurda Central Co-operative Bank
ORCB0KJR001|KEJX|urban_coop|Keonjhar Central Co-operative Bank
ORCB0KPT001|KORX|urban_coop|Koraput Central Co-operative Bank
ORCB0MYB001|MYCX|urban_coop|Mayurbhanj Central Co-operative Bank
ORCB0NGR001|NDOX|urban_coop|Nayagarh District Central Co-operative Bank
ORCB0SBP001|SDTX|district_coop|Sambalpur District Co-operative Central Bank
ORCB0SUN001|SGDX|urban_coop|Sundargarh District Central Co-operative Bank
ORCB0UPN001|UPNX|urban_coop|United Puri Nimapara Central Bank
PKGB0000001|PGBX|rrb|Pragathi Krishna Gramin Bank
PMEC0USVSBL|SVSX|urban_coop|Shri Vinayak Sahakari Bank
PUNB0HGB001|HGBX|rrb|Sarva Haryana Gramin Bank
PUNB0HPGB04|HMBX|rrb|Himachal Pradesh Gramin Bank
PUNB0MBGB06|MBGX|rrb|Dakshin Bihar Gramin Bank
PUNB0PGB003|PUGX|rrb|Punjab Gramin Bank
PUNB0RRBAGB|AGVX|rrb|Assam Gramin Vikash Bank
PUNB0RRBBGB|BGVX|rrb|Bangiya Gramin Vikash Bank
PUNB0RRBMRB|MRBX|rrb|Manipur Rural Bank
PUNB0RRBTGB|TGBX|rrb|Tripura Gramin Bank
PUNB0SUPGB5|SUBX|rrb|Prathama UP Gramin Bank
RSCB0011099|AJMX|district_coop|Ajmer Central Co-operative Bank
RSCB0012001|ALWX|urban_coop|Alwar Central Co-operative Bank
RSCB0013099|BCEX|district_coop|Banswara Central Co-operative Bank
RSCB0014099|BMCX|district_coop|Barmer Central Co-operative Bank
RSCB0015001|BHRX|urban_coop|Bharatpur Central Co-operative Bank
RSCB0016001|CBHX|district_coop|Central Co-operative Bank  Bhilwara
RSCB0017001|CEBX|district_coop|Central Co-operative Bank Bikaner
RSCB0018099|BUNX|district_coop|Bundi Central Co-operative Bank
RSCB0019099|CHKX|district_coop|Chittorgarh Kendriya Sahakari Bank
RSCB0020001|CHCX|district_coop|Churu Central Co-operative Bank
RSCB0021099|DUNX|district_coop|Dungarpur Central Co-operative Bank
RSCB0022099|JCCB|urban_coop|Jaipur Central Co-operative Bank
RSCB0023012|JLCX|urban_coop|Jalore Central Co-operative Bank Jalore
RSCB0024099|JKEX|district_coop|Jhalawar Kendriya Bank
RSCB0025099|JHUX|district_coop|Jhunjhunu Kenddriya Sahakari Bak
RSCB0026099|JODX|district_coop|Jodhpur Central Co-operative Bank
RSCB0027001|KCEX|district_coop|Kota Central Co-operative Bank Kota
RSCB0028099|NAUX|district_coop|Nagaur Central Co-operative Bank
RSCB0029099|PACX|urban_coop|Pali Central Co-operative Bank
RSCB0030099|SAWX|district_coop|Sawai Madhopur Kendriya Sahakari Bank
RSCB0031099|SIBX|district_coop|Sikar Kendriya Sahakari Bank
RSCB0032099|SCBX|district_coop|Sirohi Central Co-operative Bank
RSCB0033099|GKNX|district_coop|Ganganagar Kendriya Sahakari Bank
RSCB0034099|CTOX|district_coop|Central Co-operative Bank Tonk
RSCB0035008|UCCX|district_coop|Udaipur Central Co-operative Bank
RSCB0036099|TJAX|urban_coop|Jaisalmer Central Co-operative Bank
RSCB0037099|DKSX|urban_coop|Dausa Kendriya Bank
RSCB0038099|HANX|district_coop|Hanumangarh Kendriya Sahakari Bank
RSCB0039099|BKSX|district_coop|Baran Kendriya Sahakari Bank Baran
SBIN0000DOP|GPOX|public|General Post Office
SBIN0RRARGB|APRX|rrb|Arunachal Pradesh Rural Bank
SBIN0RRCHGB|CGBX|rrb|Chhattisgarh Rajya Gramin Bank
SBIN0RRDCGB|TGRB|rrb|TELANGANA GRAMEENA BANK
SBIN0RRELGB|EDBX|rrb|Ellaquai Dehati Bank
SBIN0RRMBGB|MADX|rrb|Madhyanchal Gramin Bank
SBIN0RRMEGB|MERX|rrb|Meghalaya Rural Bank
SBIN0RRMIGB|MZRX|rrb|Mizoram Rural Bank
SBIN0RRMRGB|MDGX|rrb|Rajasthan Marudhara Gramin Bank
SBIN0RRNLGB|NAGX|rrb|Nagaland Rural Bank
SBIN0RRPUGB|PURX|rrb|Purvanchal Gramin Bank
SBIN0RRSRGB|SAGX|rrb|Saurashtra Gramin Bank
SBIN0RRUKGB|UGBX|rrb|Utkal Grameen Bank
SBIN0RRUTGB|UTGX|rrb|Uttarakhand Gramin Bank
SBIN0RRVCGB|VGBX|rrb|Jharkhand Rajya Gramin Bank
SRCB0001MCB|MSAX|urban_coop|Mansarovar Urban Co-operative Bank
SRCB0BCB808|BDDX|urban_coop|Bhadradri Co-operative Urban Bank
SRCB0LUB262|LUCX|urban_coop|Lucknow Urban Co-operative Bank
SRCB0SAM001|SAMX|urban_coop|Samata Sahakari Bank
SVCB0001002|HCBL|urban_coop|HASTI Co-operative Bank
SVCB0002001|GSSX|urban_coop|Guardian Souharda Sahakari Bank Niyamita
SVCB0003002|VSBX|urban_coop|Vidya Sahakari Bank
SVCB0004003|DSUX|urban_coop|Dharamvir Sambhaji Urban Co-operative Bank
SVCB0006002|CUCX|urban_coop|Chiplun Urban Co-operative Bank
SVCB0007002|ABSB|urban_coop|Abhinav Sahakari Bank
SVCB0008002|ASBX|urban_coop|Ahmednagar Shahar Sahakari Bank Maryadit
SVCB0009002|PCOX|urban_coop|Peoples' Co-operative Bank
SVCB0010999|BHSX|urban_coop|Bharati Sahakari Bank
SVCB0011002|JAUX|urban_coop|Jaihind Urban Co-operative Bank
SVCB0012002|MAHX|urban_coop|Shree Mahalaxmi Urban Co-operative Credit Bank
SVCB0013002|SASA|urban_coop|Sahyadri Sahakari Bank
SVCB0014002|DAAX|urban_coop|Defence Accounts Co-operative Bank
SVCB0015003|ASKX|urban_coop|Arvind Sahakari Bank
SVCB0016101|WAIX|urban_coop|Wai Urban Co-operative Bank
SVCB0019002|SHGX|urban_coop|Shri Ganesh Sahakari Bank
SVCB0020002|SRGX|urban_coop|Sree Narayana Guru Co-operative Bank
SVCB0023002|SHAX|urban_coop|Shankar Nagari Sahakari Bank
SVCB0025002|SIRX|urban_coop|Sircilla Co-operative Urban Bank
SVCB0029002|SOBX|urban_coop|Secunderabad Co-operative Urban Bank
SVCB0030002|SNLX|urban_coop|Sarjeraodada Naik Shirala Sahakari Bank
SVCB0033002|HUBX|urban_coop|Hubli Urban Co-operative Bank
SVCB0035002|KOBX|urban_coop|Kopargaon Peoples Co-operative Bank
SVCB0043002|JSMX|urban_coop|Janata Sahakari Bank Amravati
TNSC0010000|CMDX|district_coop|Coimbatore District Central Co-operative Bank
TNSC0010100|DDBX|district_coop|Dharmapuri District Central Co-operative Bank
TNSC0010200|KCCX|district_coop|Kancheepuram Central Co-operative Bank
TNSC0010300|KAYX|district_coop|Kanyakumari District Central Co-operative Bank
TNSC0010400|KUMX|district_coop|Kumbakonam Central Co-operative Bank
TNSC0010500|CCCX|district_coop|Chennai Central Co-operative Bank
TNSC0010600|MDCX|district_coop|Madurai District Central Co-operative Bank
TNSC0010700|NDCX|district_coop|Nilgiris District Central Co-operative Bank
TNSC0010800|EDCX|district_coop|Erode District Central Co-operative Bank
TNSC0010900|PUDX|district_coop|Pudukottai District Central Co-operative Bank
TNSC0011000|RCDX|district_coop|Ramanathapuram District Central Co-operative Bank
TNSC0011100|SCCX|district_coop|Salem District Central Co-operative Bank
TNSC0011200|CDCX|district_coop|Cuddalore District Central Co-operative Bank
TNSC0011300|TJCX|district_coop|Thanjavur Central Co-operative Bank
TNSC0011400|TRDX|district_coop|Tiruchirapalli Dist. Cent Co-operative Bank
TNSC0011500|TIRX|district_coop|Tirunelveli District Central Co-operative Bank
TNSC0011600|VEDX|district_coop|Vellore District Central Co-operative Bank
TNSC0011700|DCBX|district_coop|Dindigul Central Co-operative Bank
TNSC0011800|VIRX|district_coop|Virudhunagar District Central Co-operative Bank
TNSC0011900|SVGX|district_coop|Sivagangai District Central Co-operative Bank
TNSC0012000|TIDX|district_coop|Thiruvannamalai District Central Co-operative Bank
TNSC0012100|THOX|district_coop|Thoothukudi District Central Co-operative Bank
TNSC0012200|VDCX|district_coop|Villupuram District Central Co-operative Bank
TNSC0015000|TNIX|urban_coop|Tamilnadu Industrial Co-operative Bank
TNSC0160101|PCSX|state_coop|Pondicherry State Co-operative Bank
UBIN0CG7999|CGGX|rrb|Chaitanya Godavari Grameena Bank
UBIN0DCBEDC|EDSX|district_coop|Ernakulam District Co-operative Bank
UCBA0RRBPBG|PASX|rrb|Paschim Banga Gramin Bank
UTIB0ACCB01|AMBX|district_coop|Ambala Central Co-operative Bank
UTIB0BCCB01|BHCX|urban_coop|Bhuj Commercial Co-operative Bank
UTIB0BHIW01|BHWX|district_coop|Bhiwani Central Co-operative Bank Bhiwani
UTIB0CSCB22|CHSX|state_coop|Chandigarh State Co-operative Bank
UTIB0FCCB01|TFCX|district_coop|Fatehabad Central Co-operative Bank
UTIB0HCCB01|HISX|district_coop|Hisar Central Co-operative Bank Hisar
UTIB0JCCB01|JJCX|district_coop|Jhajjar Central Co-operative Bank
UTIB0JIND01|JINX|district_coop|Jind Central Co-operative Bank
UTIB0JSBR01|JSKX|district_coop|Jila Sahakari Kendriya Bank Maryadit Raipur
UTIB0KAIT01|KICX|district_coop|Kaithal Central Co-operative Bank
UTIB0KCCB01|KNCX|district_coop|Karnal Central Co-operative Bank
UTIB0KURU01|KURX|district_coop|Kurukshetra Central Co-operative Bank
UTIB0NPCB01|NPCX|urban_coop|Nadiad Peoples Co-operative Bank
UTIB0PSCB01|PSCX|state_coop|Punjab State Co-operative Bank
UTIB0RCCB01|ROHX|district_coop|Rohtak Central Co-operative Bank
UTIB0REWA01|REWX|district_coop|Rewari Central Co-operative Bank
UTIB0S01SUB|SHUB|urban_coop|Shiggaon Urban Co-operative Bank
UTIB0SACBL1|ASNX|urban_coop|Ashoknagar Co-operative Bank
UTIB0SACUB1|AGUX|urban_coop|Agartala Co-operative Urban Bank
UTIB0SADC83|APCX|district_coop|Alappuzha District Co-operative Bank
UTIB0SAMC01|TAMX|urban_coop|Anand Mercantile Co-operative Bank
UTIB0SAMMCB|SAOX|urban_coop|Shimoga Arecanut Mandy Merchants Co-operative Bank
UTIB0SAMMSB|AKKB|urban_coop|Akkamahadevi Mahila Sahakari Bank Niyamit
UTIB0SAPM01|APMX|urban_coop|Ap Mahajan's Co-operative Urban Bank
UTIB0SAS001|SASX|urban_coop|S.A.S Nagar Central Co-operative Bank
UTIB0SASR01|AMCX|urban_coop|Amritsar Central Co-operative Bank
UTIB0SAZADB|AZAX|urban_coop|Azad Co-operative Bank
UTIB0SBCB01|BCCX|urban_coop|Bathinda Central Co-operative Bank
UTIB0SBDCC1|BEDX|district_coop|Beed District Central Co-operative Bank
UTIB0SBLDB1|TBDX|district_coop|Buldana District Central Co-operative Bank
UTIB0SBMCB1|BMCB|urban_coop|Bombay Mercantile Co-operative Bank
UTIB0SBNSBM|BNBX|urban_coop|Betul Nagrik Sahakari Bank Mydt
UTIB0SBPCB1|TBHX|urban_coop|Bhadran People's Co-operative Bank
UTIB0SBPP02|SBPX|urban_coop|Sardar Bhiladwala Pardi People's Co-operative Bank
UTIB0SBPSBN|SBPS|urban_coop|Sri Basaveshwar Pattana Sahakari Bank
UTIB0SBUBRN|SBCR|urban_coop|Shree Basaveshwar Urban Co-operative Bank
UTIB0SBUCB1|BHTX|urban_coop|Bhatkal Urban Co-operative Bank
UTIB0SCCB01|SCPX|urban_coop|Sangrur Central Co-operative Bank
UTIB0SCHAIT|CMSV|urban_coop|Chaitanya Mahila Sahakari Bank, Vijayapur
UTIB0SCJMSB|CJMX|urban_coop|Karnataka Mahila Sahakari Bank
UTIB0SCPCB1|CHPX|urban_coop|Chopda Peoples Co-operative Bank
UTIB0SCPSBN|CPSN|urban_coop|Chikmagalur Pattana Sahakara Bank Niyamitha
UTIB0SDCCB1|DHBX|district_coop|Dhanbad Central Co-operative Bank
UTIB0SDUCB3|DAUX|urban_coop|Dausa Urban Co-operative Bank
UTIB0SFAZ01|FCBX|urban_coop|Fazilka Central Co-operative. Bank
UTIB0SFCB01|FRIX|district_coop|Faridabad Central Co-operative Bank
UTIB0SFCB02|FZCX|urban_coop|Ferozepur Central Co-operative. Bank
UTIB0SFDK03|FCCX|urban_coop|Faridkot Central Co-operative Bank
UTIB0SFGH01|FSCX|urban_coop|Fatehgrah Sahib Central Co-operative Bank
UTIB0SGCB01|GUOX|district_coop|Gurgaon Central Co-operative Bank
UTIB0SGDC01|TGDX|district_coop|Gondia District Central Co-operative Bank Gondia
UTIB0SGDS01|GCCX|urban_coop|Gurdaspur Central Co-operative Bank
UTIB0SGUB02|GUNX|urban_coop|Guntur Co-operative Urban Bank
UTIB0SGUCB1|GDUX|urban_coop|Godhra Urban Co-operative Bank
UTIB0SHDCCB|HSDX|district_coop|Hassan District Co-operative Central Bank
UTIB0SHSP01|HOCX|urban_coop|Hoshiarpur Central Co-operative Bank
UTIB0SHUCBH|HUCB|urban_coop|Hadagali Urban Co-operative Bank
UTIB0SHUCBL|HUCH|urban_coop|Hanamasagar Urban Co-operative Bank
UTIB0SICB25|ILCB|urban_coop|Ilkal Co-operative Bank
UTIB0SIDB99|IDUX|district_coop|Idukki District Co-operative Bank
UTIB0SIPSB1|ISBX|urban_coop|Indraprastha Sehkari Bank
UTIB0SIRS01|SICX|district_coop|Sirsa Central Co-operative Bank
UTIB0SIUCB1|IMPX|urban_coop|Imphal Urban Co-operative Bank
UTIB0SJAL01|JCCX|urban_coop|Jalandhar Central Co-operative Bank
UTIB0SJCB01|JAMX|urban_coop|Jamia Co-operative Bank
UTIB0SJKCB1|JKSX|state_coop|J&k State Co-operative Bank
UTIB0SJSA01|JKAX|district_coop|Jila Sahakari Kendriya Bank Mydt Ambikapur
UTIB0SJSB01|JBIX|district_coop|Jila d Kendriya Bank Maryadit Bilaspur
UTIB0SJSD01|JMYX|district_coop|Jila Sahakari Kendriya Bank Mydt Durg
UTIB0SJSJ01|JKMX|district_coop|Jila Sahakari Kendriya Bank Maryadit Jagdalpur
UTIB0SJSN01|JRNX|district_coop|Jila Sahakari Kendriya Bank Maryadit Rajnandgaon
UTIB0SKCU78|TKTX|urban_coop|Kottakkal Co-operative Urban Bank
UTIB0SKDB88|KTBX|district_coop|Kottayam District Co-operative Bank
UTIB0SKDC01|TKDX|district_coop|Kannur District Co-operative Bank
UTIB0SKMCBL|KMCX|urban_coop|Krishna Mercantile Co-operative Bank
UTIB0SKNS01|SHNX|urban_coop|Shri Kanyaka Nagari Sahakari Bank
UTIB0SKNSB2|KNSX|urban_coop|Kisan Nagari Sahakari Bank Maryadit Parbhani
UTIB0SKPT01|KTCX|urban_coop|Kapurthala Central Co-operative Bank
UTIB0SKRN01|KRTX|urban_coop|Kranthi Co-operative Urban Bank
UTIB0SKU399|KUBX|urban_coop|Kaduthuruthy Urban Co-operative Bank
UTIB0SLDH01|LCCX|urban_coop|Ludhiana Central Co-operative Bank
UTIB0SLDP47|LDPX|urban_coop|Loknete Dattaji Patil Sahakari Bank
UTIB0SLNSB1|LNSX|urban_coop|Lunawada Nagarik Sahakari Bank
UTIB0SMCB01|MOGX|urban_coop|Moga Central Co-operative Bank
UTIB0SMCC01|MCCX|urban_coop|Muktsar Central Co-operative Bank
UTIB0SMCCB1|MCLX|district_coop|Mahendragarh Central Co-operative Bank
UTIB0SMLCBK|MLCG|urban_coop|Merchants Liberal Co-operative Bank
UTIB0SMMCB1|MAMX|urban_coop|Mandvi Mercantile Co-operative Bank
UTIB0SMNSB1|MGSX|urban_coop|Mangaldai Nagar Samabai Bank
UTIB0SMPCB1|MPCX|urban_coop|Moirang Primary Co-operative Bank
UTIB0SMSA01|MNCX|urban_coop|Mansa Central Co-operative Bank
UTIB0SMUC12|MHCX|urban_coop|Mahoba Urban Co-operative Bank Mahoba
UTIB0SMUCB1|MCOX|urban_coop|Mahesh Urban Co-operative Bank
UTIB0SMUCBG|MUCG|urban_coop|Merchants Urban Co-operative Bank
UTIB0SNCB01|NBBX|urban_coop|National Co-operative Bank Bangalore
UTIB0SNDCC1|NANX|district_coop|Nanded District Central Co-operative Bank
UTIB0SNRP03|NRMX|urban_coop|Northern Railway Multi State Primary Co-operative Bank
UTIB0SNSCB1|NSCX|state_coop|Nagaland State Co-operative Bank
UTIB0SNUC01|NALX|urban_coop|National Urban Co-operative Bank
UTIB0SNUCB1|NBCX|urban_coop|Nalbari Urban Co-operative Bank
UTIB0SNWS01|NCBX|urban_coop|Nawanshahr Central Co-operative Bank
UTIB0SONE01|SNPX|district_coop|Sonepat Central Co-operative Bank
UTIB0SPCB01|PTCX|urban_coop|Patiala Central Co-operative Bank
UTIB0SPKD21|PKDX|district_coop|Palakkad District Co-operative Bank
UTIB0SPKL01|PCCX|district_coop|Panchkula Central Co-operative Bank
UTIB0SPSB01|PLUX|urban_coop|Palus Sahakari Bank
UTIB0SRCB01|RCCX|urban_coop|Ropar Central Co-operative Bank
UTIB0SRECB1|RAEX|urban_coop|Railway Employees Co-operative Bank
UTIB0SRUCB1|RUCX|urban_coop|Rajasthan Urban Co-operative Bank
UTIB0SSMC01|SARX|urban_coop|Sardarganj Mercantile Co-operative Bank
UTIB0SSNS01|SANX|urban_coop|Saibaba Nagari Sahakari Bank
UTIB0SSNSBK|SNSV|urban_coop|Sarakari Naukarara Sahakari Bank Niyamt Vijayapura
UTIB0SSNSBL|SENX|urban_coop|Sehore Nagrik Sahakari Bank Sehore
UTIB0SSSB01|SGLX|urban_coop|Sangli Sahakari Bank
UTIB0SSSBL1|SISX|urban_coop|Sanmati Sahakari Bank
UTIB0SSUCB1|SSWX|urban_coop|Sundarlal Sawaji Urban Co-operative Bank
UTIB0SSVMCB|SVMC|urban_coop|Shri Vijay Mahantesh Co-operative Bank
UTIB0STAUCB|TAUB|urban_coop|Ankola Urban Co-operative Bank
UTIB0STMNBI|MUNX|urban_coop|Mahudha Nagarik Sahakari Bank
UTIB0STTN01|TTCX|urban_coop|Tarn Taran Central Co-operative Bank
UTIB0SUBBLR|BUCL|urban_coop|Belur Urban Co-operative Bank
UTIB0SVNS01|VANX|urban_coop|Vaishya Nagari Sahakari Bank
UTIB0SWJP01|WZUX|urban_coop|Wardha Zilla Parishad Emp Urban Co-operative Bank
UTIB0YCCB01|YNCX|district_coop|Yamuna Nagar Central Co-operative Bank
VCOB0000001|VIJX|urban_coop|Vijay Co-operative Bank
WBSC0BDCB01|BADX|district_coop|Bankura District Central Co-operative Bank
WBSC0BIRB02|BBDX|district_coop|Birbhum District Central Co-operative Bank
WBSC0BUCB01|BNCX|district_coop|Bhatpara Naihati Co-operative Bank
WBSC0DDCB01|DDDX|district_coop|Dakshin Dinajpur District Central Co-operative Bank
WBSC0DJCB01|DDCX|district_coop|Darjeeling District Central Co-operative Bank
WBSC0HCCB01|THWX|district_coop|Howrah District Central Co-operative Bank
WBSC0HDCB01|HOOX|district_coop|Hooghly District Central Co-operative Bank
WBSC0JCCB01|JPCX|district_coop|Jalpaiguri Central Co-operative Bank
WBSC0MALD15|MLDX|district_coop|Malda District Central Co-operative Bank
WBSC0MCCB01|MURX|district_coop|Murshidabad District Central Co-operative Bank
WBSC0MGCB01|MBCX|district_coop|Mugberia Central Co-operative Bank
WBSC0NDCB01|NDDX|district_coop|Nadia District Central Co-operative Bank
WBSC0PCCB01|PCCB|urban_coop|Purulia Central Co-operative Bank
WBSC0RCCB03|RJCX|district_coop|Raiganj Central Co-operative Bank
WBSC0TCCB01|TGCX|urban_coop|Tamluk-ghatal Central Co-operative Bank
WBSC0VCCB01|VIDX|district_coop|Vidyasagar Central Co-operative Bank
YESB0ACB002|TACX|urban_coop|Adinath Co-operative Bank
YESB0ADB001|AVDX|district_coop|Amravati District Central Co-operative Bank
YESB0ANBL01|ABDX|urban_coop|Dr. Ambedkar Nagrik Sahakari Bank Mydt Gwalior
YESB0ARCB01|ARCX|state_coop|Arunachal Pradesh State Co-operative Apex Bank
YESB0ASCB02|ASOX|urban_coop|Associate Co-operative Bank
YESB0AUBHO1|AKOX|urban_coop|Akola Urban Co-operative Bank
YESB0AUCB01|AJNX|urban_coop|Ajantha Urban Co-operative Bank
YESB0AURDCC|ABCX|district_coop|Aurangabad District Central Co-operative Bank
YESB0AZSB01|AZSX|district_coop|Almora Zila d Bank
YESB0BCBL02|BUSX|urban_coop|Business Co-operative Bank
YESB0BHUC01|BHDX|district_coop|Bhadohi Urban Co-operative Bank Gyanpur
YESB0BMSB01|BHMX|urban_coop|Brahmadeodada Mane Sahakari Bank Solapur
YESB0BNKCCB|BNKX|urban_coop|Banki Central Co-operative Bank
YESB0BRCB01|BRCX|urban_coop|Bhavana Rishi Co-operative Urban Bank
YESB0BSCB01|TBSX|state_coop|Bihar State Co-operative Bank
YESB0BUCB01|BDUX|urban_coop|Banda Urban Co-operative Bank
YESB0CB0001|CURX|urban_coop|Citizens Urban Co-operative Bank
YESB0CDC016|CPDX|district_coop|Chandrapur District Central Co-operative Bank
YESB0CJ0101|CJAX|urban_coop|Citizens' Co-operative Bank Jammu
YESB0DCBU01|UTZX|district_coop|Uttarkashi Zila Sahakari Bank
YESB0DCC001|ODCX|district_coop|Osmanabad District Central Co-operative Bank
YESB0DEVB01|DEVX|urban_coop|Development Co-operative Bank Kanpur
YESB0DNB002|DENS|urban_coop|Delhi Nagrik Sehkari Bank
YESB0DUCB01|UCDX|urban_coop|Urban Co-operative Bank Dehradun
YESB0DZSB02|DDHX|district_coop|Dehradun District Co-operative Bank
YESB0FAIZBK|FMCX|urban_coop|Faiz Mercantile Co-operative Bank, Nasik
YESB0FINCO2|FINX|urban_coop|Financial Co-operative Bank
YESB0GCUB01|GNCX|urban_coop|Gandhi Co-operative Urban Bank
YESB0GMCB01|GMBX|urban_coop|Gandhidham Mercantile Co-operative Bank
YESB0GSB001|GSBX|district_coop|Gandhibag Sahakari Bank  Nagpur
YESB0GSCB02|GSBL|state_coop|Goa State Co-operative Bank
YESB0GUCB01|GUBX|urban_coop|Godavari Urban Co-operative Bank
YESB0HCBL01|HCLX|urban_coop|HCBL Co-operative Bank
YESB0HSBLM0|HSBM|urban_coop|Haveli Sahakari Bank
YESB0HSCB01|HSSX|urban_coop|Hindustan Shipyard Staff Co-operative Bank
YESB0ICBL01|INCX|urban_coop|Indapur Urban Co-operative Bank
YESB0ICMB02|ICMX|urban_coop|Indore Cloth Market Co-operative Bank
YESB0IUBL01|IMCX|urban_coop|Imperial Urban Co-operative Bank Jalandhar
YESB0IUCB01|IUCX|urban_coop|Imperial Urban Co-operative Bank
YESB0JCBL01|JSBX|urban_coop|Janaseva Co-operative Bank (Nashik)
YESB0JCCB01|JGCX|district_coop|Jogindra Central Co-operative Bank
YESB0JIVAN1|JVCX|urban_coop|Jivan Commercial Co-operative Bank
YESB0JLCB01|JALX|urban_coop|Janalaxmi Co-operative Bank
YESB0KCCB01|KTDX|district_coop|Katihar District Central Co-operative Bank
YESB0KCUB01|KCUB|urban_coop|Khattri Co-operative Urban Bank
YESB0KDCC01|KARX|district_coop|Kaira District Central Co-operative Bank
YESB0KHBKGB|KHAX|urban_coop|Khalilabad Nagar d Bank
YESB0KLMDCB|KLMX|district_coop|Kollam District Co-operative Bank
YESB0KMNSB2|KMNX|urban_coop|Kota Mahila Nagrik Sahakari Bank
YESB0KNB002|KDIX|urban_coop|Shree Kadi Nagarik Sahakari Bank
YESB0KNUCB1|KUOB|urban_coop|Konark Urban Co-operative Bank
YESB0LMNSB2|LMNX|urban_coop|Laxmi Mahila Nagrik Sahakari Bank Maryadit
YESB0LNSB01|LOKX|urban_coop|Lokvikas Nagari Sahakari Bank Aurangabad
YESB0LUBLMP|LKHX|urban_coop|Lakhimpur Urban Co-operative Bank
YESB0MAB001|MIZX|state_coop|Mizoram Co-operative Apex Bank
YESB0MAN001|MDMX|urban_coop|Mann Deshi Mahila Sahakari Bank
YESB0MCA001|MYAX|state_coop|Meghalaya Co-operative Apex Bank
YESB0MIEB02|MAKX|urban_coop|Makarpura Industrial Estate Co-operative Bank
YESB0MNSB01|MHNX|urban_coop|Mahanagar Nagrik Sahakari Bank Maryadit
YESB0MSB002|UMSX|urban_coop|Udaipur Mahila Samridhi Urban Co-operative Bank
YESB0MSCB01|MSCX|state_coop|Manipur State Co-operative Bank
YESB0MUDC01|MZCX|urban_coop|Mizoram Urban Co-operative Development Bank
YESB0MWCB01|MAWX|urban_coop|Manipur Womens Co-operative Bank
YESB0NBGKPI|NSGX|urban_coop|Nagar Sahakari Bank Gorakhpur
YESB0NCB001|NOBX|urban_coop|Noble Co-operative Bank
YESB0NCCB01|NWCX|district_coop|Nawada Central Co-operative Bank
YESB0NDCB01|NAIX|district_coop|Nainital District Co-operative Bank
YESB0NRDB01|NRDX|urban_coop|Nasik Road Deolali Vyapari Sahakari Bank
YESB0NSB001|NGBX|urban_coop|Nagar Sahakari Bank Maharajganj
YESB0NVSB01|NVSX|urban_coop|Nagar Vikas Sahakari Bank
YESB0OMKAR1|ONSX|urban_coop|Omkar Nagreeya Sahakari Bank
YESB0PCUB01|PCUX|urban_coop|Pochampally Co-operative Urban Bank
YESB0PDBHO1|PARX|district_coop|Parbhani District Central Co-operative Bank
YESB0PDCCBL|PDBX|district_coop|Purnea District Central Co-operative Bank
YESB0PKUB01|PKBX|urban_coop|Panchkula Urban Co-operative Bank Lmited
YESB0PMCB02|PCMX|urban_coop|Panchsheel Mercantile Co-operative Bank
YESB0PMNSB1|PMNX|urban_coop|Pragati Mahila Nagrik Sahakari Bank Bhilai
YESB0PSB002|PSBX|urban_coop|Pragati Sahakari Bank
YESB0PSCB01|PRPX|urban_coop|Paraspar Sahayak Co-operative Bank
YESB0PUB001|PUCX|urban_coop|Pusad Urban Co-operative Bank
YESB0PUCB01|PNCX|urban_coop|Panipat Urban Co-operative Bank
YESB0RAJ001|TCBX|urban_coop|Co-operative Bank of Rajkot
YESB0RCUB01|RCMX|urban_coop|Ravi Commercial Urban Co-operative Bank
YESB0RECBS1|RECX|urban_coop|Railway Employees Co-operative Banking Society
YESB0RLB001|RAUX|urban_coop|Rajlaxmi Urban Co-operative Bank
YESB0RSBL01|RRSX|urban_coop|Ramrajya Sahakari Bank
YESB0RUCB05|RAJX|urban_coop|Rajsamand Urban Co-operative Bank
YESB0SATYA0|SATX|urban_coop|Adv. Shamraoji Shinde Satyashodhak Bank
YESB0SCB003|SKCX|urban_coop|Shrikrishna Co-operative Bank
YESB0SCUB01|SHMX|urban_coop|Sanghamitra Co-operative Urban Bank
YESB0SDC001|SOLX|district_coop|Solapur District Central Co-operative Bank
YESB0SHBK01|SSHX|urban_coop|Shree Sharada Sahakari Bank
YESB0SIND01|SNDX|urban_coop|Sind Co-operative Urban Bank
YESB0SLMCB2|SBLX|urban_coop|Shubhalakshmi Mahila Co-operative Bank
YESB0SMBLHO|SHRX|urban_coop|Shree Mahesh Co-operative Bank Nashik
YESB0SMCB05|SUMX|urban_coop|Surat Mercantile Co-operative Bank
YESB0SNSB01|SNSX|urban_coop|Smriti Nagrik Sahakari Bank
YESB0SSBL01|SVOX|urban_coop|Sarvodaya Sahakari Bank
YESB0SUCB01|SHCX|urban_coop|Shimla Urban Co-operative Bank
YESB0SVSB01|SVRX|urban_coop|Shree Vardhaman Sahakari Bank
YESB0TCB002|TESX|urban_coop|Textile Co-operative Bank of Surat
YESB0TMNS02|MANX|urban_coop|Mansa Nagarik Sahakari Bank
YESB0TPBL01|TRAX|urban_coop|Transport Co-operative Bank
YESB0TPCB02|PWUX|urban_coop|Parwanoo Urban Co-operative Bank
YESB0TPUCB1|PUUX|urban_coop|Puri Urban Co-operative Bank
YESB0TURA01|TURX|urban_coop|Tura Urban Co-operative Bank
YESB0UCBR01|UROX|urban_coop|Urban Co-operative Bank Rourkela
YESB0UMA002|UMAX|urban_coop|Uma Co-operative Bank
YESB0UMCBHO|UNMX|urban_coop|United Mercantile Co-operative Bank
YESB0UMIB01|UMUX|urban_coop|Umiya Urban Co-operative Bank
YESB0VASB01|VASX|urban_coop|Vyaparik Audhyogik Sahakari Bank
YESB0VCA001|VCAX|urban_coop|Vaish Co-operative Adarsh Bank
YESB0VMCB04|VMMX|urban_coop|Vaijapur Merchants Co-operative Bank
YESB0VMUB04|VMUX|urban_coop|Vidarbha Merchants Urban Co-operative Bank
YESB0VNB001|VCNB|urban_coop|Vaish Co-operative New Bank
YESB0VNSB01|VIKX|district_coop|Vikramaditya Nagrik Sahakari Bank
YESB0VSBL02|VYAX|urban_coop|Vyavsayak Sahakari Bank
YESB0VSV001|VSVX|urban_coop|Vaishali Shahari Vikas Co-operative Bank
YESB0WANA01|WNBX|urban_coop|Wana Nagirik Sahakari Bank
YESB0WUCB01|WRCX|urban_coop|Warangal Urban Co-operative Bank
YESB0YDC001|YADX|district_coop|Yavatmal District Central Co-operative Bank
YESB0YLNS01|YLNX|urban_coop|Yadagiri Lakshmi Narsimha Swamy Co-operative Urban Bank
YESB0ZSBL15|ZISX|district_coop|Zila Sahakari Bank Lucknow
"""
_OLD_CODE_TYPES = {
    "ABHYUDAYA": "urban_coop", "AIRTEL": "payments", "APMAHESH": "urban_coop", "AU": "small_finance",
    "AXIS": "private", "BANDHAN": "private", "BASSEIN": "urban_coop", "BOB": "public", "BOI": "public",
    "BOM": "public", "CANARA": "public", "CAPITAL": "small_finance", "CENTRAL": "public", "CITI": "foreign",
    "COSMOS": "urban_coop", "CSB": "private", "CUB": "private", "DBS": "foreign", "DCB": "private",
    "DHANLAXMI": "private", "DNSB": "urban_coop", "EQUITAS": "small_finance", "ESAF": "small_finance",
    "FEDERAL": "private", "FINO": "payments", "HDFC": "private", "HSBC": "foreign", "ICICI": "private",
    "IDBI": "private", "IDFC": "private", "INDIAN": "public", "INDUSIND": "private", "IOB": "public",
    "IPPB": "payments", "JANA": "small_finance", "JIO": "payments", "JK": "private", "KALUPUR": "urban_coop",
    "KARNATAKA": "private", "KJSB": "urban_coop", "KOTAK": "private", "KVB": "private", "NESFB": "small_finance",
    "NKGSB": "urban_coop", "NSDL": "payments", "PAYTM": "payments", "PNB": "public", "PSB": "public",
    "RBL": "private", "SARASWAT": "urban_coop", "SBI": "public", "SBM": "foreign", "SCB": "foreign",
    "SHIVALIK": "small_finance", "SIB": "private", "SURYODAY": "small_finance", "SVC": "urban_coop",
    "TBSB": "urban_coop", "TJSB": "urban_coop", "TMB": "private", "UCO": "public", "UJJIVAN": "small_finance",
    "UNION": "public", "UTKARSH": "small_finance", "VASAIVIKAS": "urban_coop", "YES": "private",
}
# code -> public / private / small_finance / payments / foreign / local_area / rrb / state_coop / district_coop /
# urban_coop / institution (RBI, NABARD, clearing houses: not retail account banks)
BANK_TYPE: dict[str, str] = dict(_OLD_CODE_TYPES)
for _row in _PREFIXES.strip().splitlines():
    _p, _c, _t, _n = _row.split("|")
    IFSC_BANKS.setdefault(_p, (_c or _p, _n or f"Bank with IFSC prefix {_p} (name not in registry)"))
    if _t:
        BANK_TYPE.setdefault(_c or _p, _t)
# member banks that use a sponsor bank's IFSC series ('HDFC0CAACOB' is Akhand Anand Co-op Bank, not HDFC Bank):
# the account's own IFSC is looked up here first
MEMBER_IFSC: dict[str, tuple[str, str]] = {}
for _row in _MEMBER_IFSC.strip().splitlines():
    _i, _c, _t, _n = _row.split("|")
    MEMBER_IFSC[_i] = (_c, _n)
    if _t:
        BANK_TYPE.setdefault(_c, _t)

# Name fragments (lower case) -> bank code; checked only in the header region.
NAME_BANKS: list[tuple[str, str]] = [
    ("hdfc bank", "HDFC"), ("icici bank", "ICICI"), ("state bank of india", "SBI"), ("axis bank", "AXIS"),
    ("kotak mahindra", "KOTAK"), ("kotak", "KOTAK"), ("idfc first", "IDFC"), ("yes bank", "YES"),
    ("au small finance", "AU"), ("indusind", "INDUSIND"), ("federal bank", "FEDERAL"), ("bank of baroda", "BOB"),
    ("canara bank", "CANARA"), ("punjab national", "PNB"), ("union bank of india", "UNION"),
    ("central bank of india", "CENTRAL"), ("equitas", "EQUITAS"), ("airtel payments", "AIRTEL"),
    ("saraswat", "SARASWAT"), ("bank of india", "BOI"), ("idbi bank", "IDBI"), ("indian overseas", "IOB"),
    ("indian bank", "INDIAN"), ("rbl bank", "RBL"), ("bandhan bank", "BANDHAN"), ("uco bank", "UCO"),
    ("ujjivan", "UJJIVAN"), ("paytm payments", "PAYTM"), ("fino payments", "FINO"), ("bank of maharashtra", "BOM"),
    ("south indian bank", "SIB"), ("karur vysya", "KVB"), ("city union", "CUB"), ("karnataka bank", "KARNATAKA"),
    ("dcb bank", "DCB"), ("jana small", "JANA"), ("suryoday", "SURYODAY"), ("esaf", "ESAF"), ("utkarsh", "UTKARSH"),
    ("dbs bank", "DBS"), ("hsbc", "HSBC"), ("standard chartered", "SCB"), ("citibank", "CITI"),
    ("tamilnad mercantile", "TMB"), ("punjab & sind", "PSB"), ("punjab and sind", "PSB"), ("india post payments", "IPPB"),
    ("jio payments", "JIO"), ("nsdl payments", "NSDL"), ("csb bank", "CSB"), ("dhanlaxmi", "DHANLAXMI"),
    ("cosmos", "COSMOS"), ("tjsb", "TJSB"), ("sbm bank", "SBM"), ("dombivli nagari", "DNSB"),
    ("vasai vikas", "VASAIVIKAS"), ("kalyan janata", "KJSB"), ("thane bharat", "TBSB"),
]
# registry banks: only full, distinctive names (never a word that turns up in other banks' narrations, never a
# fragment that sits inside another bank's name: 'bharat co-operative bank' is inside 'shree bharat co-operative')
NAME_BANKS += [
    ("nainital bank", "NTBL"), ("jammu & kashmir bank", "JK"), ("jammu and kashmir bank", "JK"), ("j&k bank", "JK"),
    ("catholic syrian bank", "CSB"), ("lakshmi vilas bank", "LAVB"), ("laxmi vilas bank", "LAVB"),
    ("ing vysya", "VYSA"), ("allahabad bank", "ALLA"), ("andhra bank", "ANDB"), ("oriental bank of commerce", "ORBC"),
    ("united bank of india", "UTBI"), ("syndicate bank", "SYNB"), ("vijaya bank", "VIJB"), ("dena bank", "BKDN"),
    ("state bank of bikaner", "SBBJ"), ("state bank of hyderabad", "SBHY"), ("state bank of mysore", "SBMY"),
    ("state bank of travancore", "SBTR"), ("state bank of patiala", "STBP"), ("bharatiya mahila bank", "BMBL"),
    ("fincare small finance", "FSFB"), ("unity small finance", "UNBA"), ("north east small finance", "NESFB"),
    ("shivalik small finance", "SHIVALIK"), ("capital small finance", "CAPITAL"),
    ("bank of america", "BOFA"), ("barclays bank", "BARC"), ("deutsche bank", "DEUT"), ("jpmorgan chase", "CHAS"),
    ("mizuho bank", "MHCB"), ("mufg bank", "BOTM"), ("sumitomo mitsui", "SMBC"), ("societe generale", "SOGE"),
    ("bnp paribas", "BNPA"), ("credit agricole", "CRLY"), ("doha bank", "DOHB"), ("emirates nbd", "EBIL"),
    ("qatar national bank", "QNBA"), ("kookmin bank", "KBKB"), ("shinhan bank", "SHBK"), ("woori bank", "HVBK"),
    ("keb hana", "KOEX"), ("industrial and commercial bank of china", "ICBK"), ("rabobank", "RABO"),
    ("bank of bahrain", "BBKM"), ("bank of ceylon", "BCEY"), ("mashreq bank", "MSHQ"), ("united overseas bank", "UOVB"),
    ("bank of nova scotia", "NOSC"),
    ("apna sahakari", "ASBL"), ("zoroastrian co", "ZCBL"), ("mehsana urban", "MSNU"), ("rajkot nagarik", "RNSB"),
    ("ahmedabad mercantile", "AMCB"), ("greater bombay co", "GBCB"), ("new india co-operative", "NICB"),
    ("citizen credit co", "CCBL"), ("janata sahakari bank (pune)", "JSBP"), ("shamrao vithal", "SVC"),
    ("abhyudaya co", "ABHYUDAYA"), ("nkgsb", "NKGSB"), ("bassein catholic", "BASSEIN"), ("kalupur commercial", "KALUPUR"),
    ("kerala gramin", "KLGB"), ("karnataka gramin", "PKGB"), ("maharashtra gramin", "MAHG"),
    ("andhra pradesh grameena vikas", "APGV"), ("chhattisgarh rajya gramin", "CRGB"),
    ("rajasthan marudhara", "RMGB"), ("tamil nadu grama", "TNGB"), ("telangana grameena", "TGRB"),
]

CODE_TO_NAME = {code: name for code, name in IFSC_BANKS.values()}
for _c, _n in MEMBER_IFSC.values():
    CODE_TO_NAME.setdefault(_c, _n)

# the bank's own web / mail domain printed in the statement header ('Visit www.icicibank.com'); header region only
BANK_DOMAINS: dict[str, str] = {
    "icicibank.com": "ICICI", "hdfcbank.com": "HDFC", "onlinesbi.sbi": "SBI", "onlinesbi.com": "SBI",
    "sbi.co.in": "SBI", "axisbank.com": "AXIS", "kotak.com": "KOTAK", "idfcfirstbank.com": "IDFC", "yesbank.in": "YES",
    "aubank.in": "AU", "indusind.com": "INDUSIND", "federalbank.co.in": "FEDERAL", "bankofbaroda.in": "BOB",
    "bankofbaroda.com": "BOB", "canarabank.com": "CANARA", "pnbindia.in": "PNB", "unionbankofindia.co.in": "UNION",
    "centralbankofindia.co.in": "CENTRAL", "bankofindia.co.in": "BOI", "idbibank.in": "IDBI", "indianbank.in": "INDIAN",
    "iob.in": "IOB", "ucobank.com": "UCO", "bankofmaharashtra.in": "BOM", "psbindia.com": "PSB", "rblbank.com": "RBL",
    "bandhanbank.com": "BANDHAN", "southindianbank.com": "SIB", "kvb.co.in": "KVB", "cityunionbank.com": "CUB",
    "karnatakabank.com": "KARNATAKA", "dcbbank.com": "DCB", "equitasbank.com": "EQUITAS", "janabank.com": "JANA",
    "hsbc.co.in": "HSBC", "sc.com": "SCB", "jkbank.com": "JK", "csb.co.in": "CSB", "saraswatbank.com": "SARASWAT",
    "cosmosbank.com": "COSMOS", "tjsbbank.co.in": "TJSB",
}
# RBI moved banks to '<name>.bank.in' (2025): the label is the bank's short name ('kotak.bank.in')
_BANK_IN_LABELS = {"hdfc": "HDFC", "icici": "ICICI", "sbi": "SBI", "axis": "AXIS", "kotak": "KOTAK",
                   "idfcfirst": "IDFC", "yes": "YES", "au": "AU", "indusind": "INDUSIND", "federal": "FEDERAL",
                   "bankofbaroda": "BOB", "canara": "CANARA", "pnb": "PNB", "unionbank": "UNION", "iob": "IOB",
                   "rbl": "RBL", "bandhan": "BANDHAN", "dcb": "DCB", "equitas": "EQUITAS", "sbm": "SBM",
                   "kvb": "KVB"}
DOMAIN_RE = re.compile(r"(?:www\.|@|//)([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+)", re.I)


def _domain_code(text: str) -> str | None:
    """Bank whose own domain the header prints; None when no domain or domains of two different banks."""
    codes = set()
    for m in DOMAIN_RE.finditer(text or ""):
        host = m.group(1).lower().rstrip(".")
        if host.endswith(".bank.in"):
            code = _BANK_IN_LABELS.get(host[:-len(".bank.in")].split(".")[-1])
        else:
            code = next((c for d, c in BANK_DOMAINS.items() if host == d or host.endswith("." + d)), None)
        if code:
            codes.add(code)
    return codes.pop() if len(codes) == 1 else None

IFSC_RE = re.compile(r"\b([A-Z]{4})0([A-Z0-9]{6})\b")
OWN_IFSC_RE = re.compile(r"IFS(?:C|\s*Code|C\s*Code)?\s*(?:Code)?\s*(?:\(.*?\))?\s*[:\-]?\s*([A-Z]{4}0[A-Z0-9]{6})", re.I)


def _name_code(low: str) -> str | None:
    """Earliest bank-name fragment in a lower-cased text ('state bank of india' beats 'bank of india')."""
    hits = [(low.find(k), -len(k), code) for k, code in NAME_BANKS if k in low]
    return min(hits)[2] if hits else None


# glossary line naming the issuing bank: ICICI 'INFT - Internal Fund Transfer (Within ICICI Bank)'
SELF_REF_RE = re.compile(r"\(\s*within\s+([A-Za-z][A-Za-z .&]{2,40}?)\s*\)", re.I)
# words that may sit next to the bank name on a letterhead line ('SBM Bank (India)', 'DCB BANK LIMITED')
_LETTERHEAD_FILLER = {"bank", "ltd", "limited", "india", "the", "co", "op", "operative", "sahakari", "of", "small",
                      "finance", "payments", "first", "mahindra", "and", "&"}


def _doc_bank(doc) -> tuple[str, str] | None:
    """Bank named by the document itself when the header text names none: a glossary self-reference, or a
    letterhead line (just the bank's name) printed at the same spot on most pages. Narrations mention other banks
    constantly, so a free-text bank name anywhere in the body is never used."""
    n = doc.page_count
    for pno in sorted({n - 1, n - 2, 0} & set(range(n))):
        for m in SELF_REF_RE.finditer(doc[pno].get_text()):
            code = _name_code(m.group(1).lower())
            if code:
                return code, "self-reference"
    # the bank's own service domain / e-mail on the first or last page (KVB prints no IFSC or name on page 1,
    # only 'eservice@kvb.bank.in' in the last-page notes); None when two different banks' domains appear
    code = _domain_code(doc[0].get_text() + "\n" + doc[n - 1].get_text()) if n else None
    if code:
        return code, "doc-domain"
    pages = list(range(min(n, 8)))
    if len(pages) < 2:
        return None
    seen: dict[tuple[str, int], set[int]] = {}
    for pno in pages:
        lines: dict[tuple[int, int], list] = {}
        for w in doc[pno].get_text("words"):
            lines.setdefault((w[5], w[6]), []).append(w)
        for ws in lines.values():
            text = " ".join(w[4] for w in ws).lower()
            toks = re.findall(r"[a-z&]+", text)
            if not toks or len(toks) > 6 or re.search(r"\d", text):
                continue
            code = _name_code(" ".join(toks))
            if not code:
                continue
            key = next(k for k, c in NAME_BANKS if c == code and k in " ".join(toks))
            rest = [t for t in toks if t not in key.split() and t not in _LETTERHEAD_FILLER]
            if rest:
                continue
            seen.setdefault((code, round(min(w[1] for w in ws) / 4)), set()).add(pno)
    best = max(seen.items(), key=lambda kv: len(kv[1]), default=None)
    if best and len(best[1]) >= max(2, 0.6 * len(pages)):
        return best[0][0], "letterhead"
    return None


ACCT_NO_RE = re.compile(r"\b(?:a/c|account)\s*(?:no\.?|number|num)?\s*[:.\-]?\s*(\d{9,18})\b", re.I)


def _own_account_bank(header_text: str, doc) -> str | None:
    """The header's full account number glued to an IFSC prefix in the body: 'IMPSAPITJSB051110100000539' is an IMPS
    into this very account, so the prefix is the account's bank. Needs >=2 hits and a single bank."""
    accts = {m.group(1) for m in ACCT_NO_RE.finditer(header_text or "")}
    if len(accts) != 1:
        return None
    acct_re = re.compile(r"([A-Z]{4})" + next(iter(accts)) + r"(?!\d)")
    hits = Counter()
    for pno in range(doc.page_count):
        for m in acct_re.finditer(doc[pno].get_text()):
            if m.group(1) in IFSC_BANKS:
                hits[IFSC_BANKS[m.group(1)][0]] += 1
    if len(hits) == 1 and sum(hits.values()) >= 2:
        return next(iter(hits))
    return None


def detect_bank(header_text: str, page1_text: str = "", narrations: str = "", doc=None) -> tuple[str, str, str]:
    """Return (code, display_name, method). code='UNKNOWN' if nothing found."""
    own_unlisted = None
    for m in OWN_IFSC_RE.finditer(header_text or ""):
        if m.group(1).upper() in MEMBER_IFSC:  # co-op / RRB member bank on a sponsor bank's IFSC series
            code, name = MEMBER_IFSC[m.group(1).upper()]
            return code, name, "ifsc-member"
        pref = m.group(1)[:4].upper()
        if pref in IFSC_BANKS:
            code, name = IFSC_BANKS[pref]
            return code, name, "ifsc"
        own_unlisted = own_unlisted or pref
    code = _name_code((header_text or "").lower())
    if code:
        return code, CODE_TO_NAME.get(code, code), "header-name"
    code = _domain_code(header_text)
    if code:
        return code, CODE_TO_NAME.get(code, code), "header-domain"
    if doc is not None:
        hit = _doc_bank(doc)
        if hit:
            return hit[0], CODE_TO_NAME.get(hit[0], hit[0]), hit[1]
    if own_unlisted:  # the account's own IFSC is printed but not in our map: never guess from narration IFSCs
        return "UNKNOWN", f"Unknown bank (IFSC {own_unlisted})", "ifsc-unlisted"
    if doc is not None:
        code = _own_account_bank(header_text, doc)
        if code:
            return code, CODE_TO_NAME.get(code, code), "own-account-ref"
    # narration IFSCs: skip UPI pseudo-codes ('YESB0YBLUPI', 'HDFC0MERUPI') and refuse a tie between banks
    prefixes = Counter(m.group(1) for m in IFSC_RE.finditer(page1_text or "")
                       if m.group(1) in IFSC_BANKS and "UPI" not in m.group(2))
    top = prefixes.most_common(2)
    if top and (len(top) == 1 or top[0][1] > top[1][1]):
        code, name = IFSC_BANKS[top[0][0]]
        return code, name, "ifsc-frequency"
    own = _own_ref_bank(narrations)
    if own:
        code, name = IFSC_BANKS[own]
        return code, name, "own-neft-ref"
    # last resort: the bank's own name printed as a short stand-alone line on page 1 (page footer such as
    # 'SBM Bank (India)'); narration fragments carry digits / ids / UPI handles ('.../q123@ybl/Yes Bank'),
    # so lines with digits, '/' or '@' are ignored
    names = Counter()
    for line in (page1_text or "").lower().splitlines():
        if re.search(r"[\d/@]", line) or len(line.split()) > 5:
            continue
        hit = next((code for k, code in NAME_BANKS if k in line), None)
        if hit:
            names[hit] += 1
    if len(names) == 1:
        code = next(iter(names))
        return code, CODE_TO_NAME.get(code, code), "page-name"
    return "UNKNOWN", "Unknown bank", "none"


# ---------------------------------------------------------------- layout certification
# code -> number of real bank-generated PDFs in data/samples whose balance chain reconciles 100% (status MATCHED,
# verified 100%) with verdict ORIGINAL, counted by the bank the engine detects and checked against the PDF header
# (several samples sit in the wrong folder: HDFC_003 / UNKNOWN_003 are ICICI, IOB_001 is HDFC, UNKNOWN_002 is TJSB).
# Engine run 2026-11-17 (blind-test onboarding) with data/samples/passwords.csv. Proves the row layout only - not
# narration or analytics accuracy (audits found narration shifts on some, e.g. UNKNOWN_002/003, ICICI_001, and
# CANARA_001 which is still REVIEW). New 2026-11-17: AXIS, PNB, CANARA, BOI, CENTRAL, IDBI, UCO, FEDERAL (FEDERAL_002;
# FEDERAL_001 is PyPDF4-rewritten = FAKE), KVB, SARASWAT, AIRTEL, AU, IOB, RBL. New 2026-11-20: BOM (BOM_003,
# openhtmltopdf, 266 rows; centred cells so data_quality stays REVIEW), BANDHAN (BANDHAN_001, Apache FOP, glued
# 'June22, 2026' dates, 121 rows, READY), ESAF (ESAF_001, JasperReports, days newest first / rows oldest first, 136
# rows, READY). CANARA_002 is the Acrobat-edited Canara file (FAKE) - not counted. HSBC (HSBC_007, the monthly
# e-statement: OpenText Exstream 'Registered to: HSBCGLOB', password-protected, 85 rows, printed turnover/count
# match; date once per day -> pdf_parser._closing_amount_rows; REVIEW: no row-alignment proof yet).
CERTIFIED: dict[str, int] = {
    "HDFC": 7, "IDFC": 9, "SBI": 5, "ICICI": 6, "BOB": 1, "CUB": 1, "DCB": 1, "EQUITAS": 1, "INDIAN": 1, "JANA": 1,
    "KARNATAKA": 1, "KOTAK": 2, "SBM": 1, "SCB": 1, "SIB": 1, "TJSB": 1, "YES": 2, "UNION": 1,
    "AXIS": 2, "PNB": 1, "CANARA": 1, "BOI": 1, "CENTRAL": 1, "IDBI": 2, "UCO": 1, "FEDERAL": 1, "KVB": 1,
    "SARASWAT": 1, "AIRTEL": 1, "AU": 2, "IOB": 1, "RBL": 1, "BOM": 1, "BANDHAN": 1, "ESAF": 1, "HSBC": 1,
    "INDUSIND": 1, "DBS": 1, "FINO": 1,
}
# 2026-11-20 password batch (password = file name before '_'): INDUSIND_004 (iText 7 pdfHTML, password-protected,
# same statement as the PDFium copy INDUSIND_003 - so the 3 PDFium IndusInd files were re-saves), DBS_001 (Quadient
# Inspire 17, printed totals match), FINO_002 (iTextSharp 5.5, same statement as the PDFium FINO_001; prints
# 'Opening Balance 0.00' - see reconcile placeholder rule). UJJIVAN_002 is PyPDF2 + password = FAKE by rule (Anuj's call).
# same 100% chain, but the file was re-saved by a third-party tool (verdict FAKE): the bank's layout parses, yet the
# file itself is not bank-generated, so it does not certify the bank on its own
TOOL_EDITED_SAMPLES: dict[str, int] = {
    "AXIS": 1, "BOB": 1, "DNSB": 1, "FEDERAL": 1, "HSBC": 5, "ICICI": 2, "IDFC": 2, "INDUSIND": 3, "IOB": 1,
    "KOTAK": 1, "PSB": 1, "FINO": 1, "UJJIVAN": 1,
}  # 2026-11-20: + HSBC_005 / HSBC_006 (browser prints, 4th/5th), INDUSIND_003 (PDFium, 3rd of 3), FINO_001 and
#    UJJIVAN_001 (PDFium; FINO_001 chain 100% but its printed opening is 0.00 -> chain status PARTIAL)
# not counted anywhere: AU_001 (100% rows but chain status PARTIAL), AU_002/003, BOM_001, HDFC_001, IDFC_005,
# IOB_002, KOTAK_003, RBL_001, TBSB_001, UNKNOWN_001 (ICICI), YES_002 (ICICI) below 100% or PARTIAL; IDFC_001
# (KOTAK) SUSPICIOUS 99.3%; YES_003 one row; xls exports; 6 encrypted without password; 2 scanned (SCANNED_002 =
# DBS, Microsoft Print to PDF of page images).


def bank_certification(code: str) -> dict:
    """{'certified': bool, 'samples': n} - n = bank-generated samples that reconcile 100% (see CERTIFIED);
    'tool_edited_samples' = extra 100% files of that layout that were re-saved by a third-party tool."""
    n = CERTIFIED.get(code or "", 0)
    return {"certified": n > 0, "samples": n, "tool_edited_samples": TOOL_EDITED_SAMPLES.get(code or "", 0)}


# inward NEFT narration '<sender IFSC prefix>/<receiving bank>N<yy><ddd><seq>': 'PJSB/KARBN26217197677'
NEFT_OWN_REF_RE = re.compile(r"\b([A-Z]{4})/([A-Z]{4})N\d{8,}\b")


def _own_ref_bank(narrations: str) -> str | None:
    """Logo-only headers (no name / IFSC in the text layer). The receiving bank stamps its own NEFT reference on
    inward credits; when one ref prefix follows >=2 different sender banks on >=3 rows it is the account's bank."""
    senders: dict[str, set[str]] = {}
    hits = Counter()
    for m in NEFT_OWN_REF_RE.finditer(narrations or ""):
        snd, own = m.groups()
        if own in IFSC_BANKS and snd != own and snd not in ("NEFT", "RTGS", "IMPS"):
            senders.setdefault(own, set()).add(snd)
            hits[own] += 1
    ok = [b for b in senders if len(senders[b]) >= 2 and hits[b] >= 3]
    return ok[0] if len(ok) == 1 else None


# ---------------------------------------------------------------- keyword dictionaries
# Lenders: banks, NBFCs, digital lenders / BNPL. Matched on a normalised (upper, single-space) narration.
LENDERS = [
    "BAJAJ FIN", "BAJAJFIN", "BAJAJ FINANCE", "BFL ", "TATA CAPITAL", "TATACAPITAL", "HDB FIN", "HDBFS", "HDB FINANCIAL",
    "L&T FIN", "L AND T FIN", "LTFS", "MUTHOOT", "MANAPPURAM", "IIFL", "FULLERTON", "SMFG", "PIRAMAL", "POONAWALLA",
    "ADITYA BIRLA FIN", "ABFL", "HERO FINCORP", "HEROFINCORP", "CHOLAMANDALAM", "CHOLA", "SHRIRAM", "MAHINDRA FIN",
    "MAHINDRA & MAHINDRA FIN", "SUNDARAM FIN", "INDIABULLS", "DHANI", "HOME CREDIT", "HOMECREDIT", "CAPRI GLOBAL",
    "KREDITBEE", "KRAZYBEE", "KRAZY BEE", "MONEYVIEW", "MONEY VIEW", "WHIZDM", "NAVI FIN", "NAVI TECH", "NAVI LOAN",
    "FIBE", "FIBEAPP", "EARLYSALARY", "SOCIAL WORTH", "KISSHT", "ONEMI", "SI CREATIVA", "SLICE", "QUADRILLION", "LAZYPAY",
    "PAYU FIN", "CASHE", "BHANIX", "MPOKKET", "TRUEBALANCE", "TRUE BALANCE", "STASHFIN", "AKARA CAPITAL", "PAYSENSE",
    "MONEYTAP", "ZESTMONEY", "SIMPL", "GETSIMPL", "POSTPE", "AVAIL FIN", "INCRED", "INCREDFIN", "UGRO", "LENDINGKART", "FLEXILOAN", "INDIFI",
    "NIRA FIN", "RUPEEK", "FINNABLE", "SMARTCOIN", "OLYV", "RUPEEREDEE", "RUPEE REDEE", "MOBIKWIK ZIP", "ZIP EMI",
    "AMAZON PAY LATER", "FLIPKART PAY LATER", "AXIO", "CAPITAL FLOAT", "KOTAK PRIME", "TVS CREDIT", "TVSCREDIT",
    "MAHAVEER FIN", "AAVAS", "AADHAR HOUSING", "LIC HOUSING", "LICHFL", "PNB HOUSING", "CAN FIN", "REPCO",
    "BAJAJ HOUSING", "HDFC LTD", "SAMMAAN", "UJJIVAN LOAN", "SATIN CREDIT", "SPANDANA", "CREDITACCESS", "ASIRVAD",
    "ARMAN FIN", "SVATANTRA", "BELSTAR", "FUSION MICRO", "AROHAN", "ANNAPURNA FIN", "MIDLAND MICRO", "SONATA FIN",
    "SAMUNNATI", "MONEYWISE", "RING ", "KISHT", "BRANCH INTER", "CREDITSAISON", "CREDIT SAISON", "NIYOGIN",
    "LOANTAP", "PAYMEINDIA", "PAYME INDIA", "INSTAMONEY", "CASHBEAN", "PC FINANCIAL", "KREDITZY", "SALARYDOST",
    "RUPEELEND", "LENDBOX", "FAIRCENT", "LENDENCLUB", "LEN DEN", "INNOFIN", "DMI FIN", "NORTHERN ARC", "VIVRITI",
    # NBFCs / digital lenders seen disbursing (net of a ~2% fee) and collecting by UPI/RZP in the 2026-11 samples
    "DEVMUNI", "KASAR CREDIT", "KASARCREDIT", "AGF FINLEASE", "AGFFINLEASE", "RICHMAN FIN", "RICHMANFIN",
    "AGRIM FINCAP", "MAHAVIRA FINLEASE", "MAHAVIRAFINLEAS",
    # Shriram City Union Finance's NACH sponsor code ('ACH DR-SCUF-HSBC0002..', RBL_001)
    "SCUF",
]
# Lender names match as words: left edge always, right edge too for short single-token names
# ('RING ' must not hit 'CATERING', 'FIBE' not 'FIBER', 'INCRED' not 'INCREDIBLE').
def _lender_pat(w: str) -> str:
    k = w.strip()
    if len(k) >= 7:  # distinctive enough to match glued: 'OIDMPOKKET..', 'LAZYPAYRE', 'INNOFINSOLU'
        return re.escape(w)
    right = "" if k in _LENDER_GLUE_OK else r"(?![A-Z])"
    return r"(?<![A-Z])" + re.escape(k) + right


_LENDER_GLUE_OK = {"CASHE"}  # 'casheAeriesTec' disbursals
LENDERS += ["SIMPLONLINE", "SIMPLPAY", "BFLAUTOPAY"]  # BFLAUTOPAY: Bajaj Finance UPI-autopay handle (bflautopay@hdfcbank)
LENDER_RE = "|".join(_lender_pat(w) for w in sorted(LENDERS, key=len, reverse=True))
LOAN_WORDS = ["LOAN", " EMI", "EMI ", "/EMI", "-EMI", "EMI/", "EMI-", "LN REPAY", "INSTALMENT", "INSTALLMENT",
              "LOAN RECOVERY", "REPAYMENT", "LOAN COLL", "LOAN A/C"]
MANDATE_WORDS = ["ACH D", "ACH/", "ACH-", "NACH", "ECS", "ENACH", "E-NACH", "MANDATE", "AUTOPAY", "AUTO PAY",
                 "SI DEBIT", "SI-", "STANDING INSTRUCTION"]
DISBURSAL_WORDS = ["DISB", "DISBURSAL", "DISBURSEMENT", "LOAN CREDIT", "LOAN AMT", "LOAN AMOUNT"]
# ---- loans-only evidence (used by analytics.detect_loans; never by salary / categorize)
# disbursal phrases, incl. glued / cut fields seen in the 2026-11 audit: 'LoanDisbursa', 'Paymentdisbu',
# 'PAYMENTDISBURSED', 'TRADOFINADISBURSA L', 'LOAN DISBURS AL', 'Amount Disbursed', 'PL STF DISB', 'DISBLPPL..'
DISB_RE = r"DISBURS|DISBU\b|LOAN ?DISB|PAYMENT ?DISB|\bDISB\b|\bDISBLP|LOAN ?DI\b"
# a credit with one of these is not a loan payout even when it says 'disbursement'
NOT_DISB_WORDS = ["SALARY", "CLAIM", "INSURANCE", "SCHOLARSHIP", "PENSION", "SUBSIDY", "DIVIDEND", "REFUND", "RETURN",
                  "REVERSAL", "RVSL", "PENNY", "CASHBACK", "INCENTIVE", "COMMISSION", "REIMB", "PF ", "PROVIDENT",
                  "REPAY"]  # 'UAIBLOAN REPAYMENT' credited = someone repaying the holder
# streaming / app subscriptions paid by UPI autopay or card mandates - never a loan EMI
SUBSCRIPTION_WORDS = ["NETFLIX", "APPLE MEDIA", "APPLESERVICES", "APPLE SERVICES", "APPLE.COM", "ITUNES", "SPOTIFY",
                      "FANCODE", "YOUTUBE", "GOOGLE PLAY", "GOOGLEPLAY", "PLAYSTORE", "HOTSTAR", "JIOHOTSTAR",
                      "DISNEY", "PRIME VIDEO", "AMAZON PRIME", "PRIMEVIDEO", "SONYLIV", "ZEE5", "JIOCINEMA",
                      "OTTMANDATE", "OTT PLAY", "EXECUTION TEST", "AUDIBLE", "LINKEDIN", "MICROSOFT", "ADOBE",
                      "SUBSCRIPTION", "CHAUPAL", "PAISABAZAAR"]
# payment aggregators that collect NACH / UPI on behalf of a lender: the lender is the sub-merchant after them
AGGREGATOR_WORDS = ["RAZORPAYSOFTWAREPRIV", "RAZORPAYSOFTWAREP", "RAZORPAYSOFTWARE", "RAZORPAYSOFTWA", "CTRAZORPAY",
                    "RAZORPAY", "CASHFREE PAYMENTS IN", "CASHFREE PAYMENTS", "CASHFREE", "CSHFRE", "PAYU", "EASEBUZZ",
                    "BILLDESK", "DIGIO", "PAYNETZ", "CAMSPAY"]
# digital-lending app brands named in the 2026-11 audit findings that the generic finance-word rule cannot see
LOAN_APPS = ["CAPITALNOW", "FLEXSALARY", "VIVIFI", "NORTHERNAR", "RUPEE112", "SNAPPAISA", "SALARYONTIME",
             "FUNDSBULL", "LENDINGPLATE", "CREDNIDHI", "CASHERO", "LOANZAP", "INSTANTLOANPAY", "CASHKREDIT",
             "KREDITO24", "FINCFRIENDS", "FATAKPAY", "RUPEERAFTAAR", "SALARYTOPU", "MONEYINTIME", "PAISABOXX",
             "TRUEFUND", "TRUECREDITS", "SNAPMINT", "LIQUILOANS", "TWOMINUTESLOAN", "BLINKR", "CAPRIGLOBAL"]

CREDIT_CARD_WORDS = ["CREDIT CARD", "CREDITCARD", "CC PAYMENT", "CARD PAYMENT", "CRED CLUB", "CRED.", "@CRED",
                     "SBI CARD", "SBICARD", "AMEX", "CARD BILL", "CC BILL", "BILLDESK*CARD", "ONECARD", "UNI CARDS",
                     "HDFC CARD", "ICICI CARD", "AXIS CARD", "KOTAK CARD"]
_MONTHS_RE = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]{0,6}"
SALARY_RE = (r"\b(SALARY|SALARIES|SAL|SALRY|PAYROLL|WAGES?|STIPEND|REMUNERATION|SAL\s?CR|SAL\s?CREDIT)\b"
             r"|\b(?:SALARY|SAL)(?:" + _MONTHS_RE + r"|\d{2})\d{0,4}\b")  # 'SalaryJuly26', 'SALAUG2026', 'SALARY0726'
# a pay-period label inside a name field, not part of the name: 'SALARYJULY26', 'JULY26', 'AUG2026'
PERIOD_TOKEN_RE = r"^(?:SALARY|SAL)?" + _MONTHS_RE + r"'?\d{2,4}$|^(?:SALARY|SAL)\d{2,6}$"
COMPANY_WORDS = [" LTD", "LIMITED", " PVT", "PRIVATE", " LLP", "TECHNOLOGIES", "TECHNOLOGY", "SOLUTIONS", "SERVICES",
                 " CORP", " INC", "ENTERPRISES", "INDUSTRIES", "CONSULT", "SYSTEMS", "SOFTWARE", "INFOTECH",
                 "PHARMA", "HOSPITAL", "UNIVERSITY", "SCHOOL", "COLLEGE", "GOVT", "GOVERNMENT", "TREASURY", "MINISTRY"]
GAMBLING_WORDS = ["DREAM11", "DREAM 11", "MPL ", "MOBILE PREMIER", "MY11CIRCLE", "MY11 CIRCLE", "RUMMY", "JUNGLEE",
                  "WINZO", "POKERBAAZI", "POKER", "BETWAY", "1XBET", "PARIMATCH", "STAKE.COM", "BET365", "A23 ",
                  "HOWZAT", "GAMEZY", "ZUPEE", "LUDO", "FANTASY", "BETTING", "SPORTSBOOK", "DAFABET", "4RABET"]
# 'CASINO' removed: the only hits were a shop QR ('UPI/DR/../CASINO B/HDFC/VYAPAR', Rs 60/120, CUB_001), not betting
CRYPTO_WORDS = ["WAZIRX", "COINDCX", "COINSWITCH", "BINANCE", "ZEBPAY", "BITBNS", "MUDREX", "GIOTTUS", "CRYPTO"]
# ---- bounces (return of a mandate / cheque presented on the holder's account)
# return words as whole words ('RTN' must not hit 'PARTNER'); they need an instrument word (BOUNCE_CONTEXT_RE).
# Not 'RTD': it is also a deposit / payee name ('SI DR TO RTD A/C', 'NACH DR RTD MOTORS') - see BOM_RTD_CHAR_RE
BOUNCE_RE = (r"(?<![A-Z])(?:RETURN|RETURNED|RTN|RETN|RET|BOUNCE|BOUNCED|DISHONOU?R(?:ED)?|INSUFF[A-Z]*|UNPAID|"
             r"REJECT(?:ED)?)(?![A-Z])|REVERSAL OF ACH")
# glued bank codes that are a return on their own (seen in the 2026-11 samples): 'CHGS-ACHRet-310326-Amt29403'
# (Karnataka), 'ECSRTN1_0209..' / 'ECSRTNCHGS050923_SR..' (ICICI / Kotak / Yes), 'NACH_AD_RTN_CHRG~' (ICICI),
# 'ATD-..-RTNCHGSApr24' (Kotak), 'LPBNGXX55021-BOUNCECHGSAPR25' (ICICI loan a/c), 'NACHDR RETN' (SIB)
BOUNCE_SELF_RE = (r"ACHRET|ECSRTN|NACH_AD_RTN|RTNCHG|BOUNCE|DISHONOU?R|NACHDR RETN|ACH DR RETN|ACH DEBIT RETURN|"
                  r"ECS RETURN|NACH RETURN|NACH RTN|EMI RTN|CHQ RETURN")
# Bank of Maharashtra return charges (BOM_003, 2026-11-20): 'ECS DR RTD CHAR' 300.00 (RTD = returned) + its next row
# 'GST' 54.00, and 'RETURN CHARGES TO 97431008534 TRANSFER' 354.00 (swept to the bank's charges a/c, no instrument
# word). A charge only when DR and <= 1200; RETURN CHARGES also needs a standard GST-inclusive amount (a customer's
# own 'RETURN CHARGES' transfer remark of 850 / 2500 is not one)
BOM_RTD_CHAR_RE = r"(?<![A-Z])(?:ECS|ACH|NACH|SI) DR RTD CHA?RG?(?:ES?)?(?![A-Z])"
BOM_RETURN_CHARGES_RE = r"^[-.\s]*RETURN CHARGES?(?![A-Z])"
BOUNCE_CONTEXT_RE = (r"(?<![A-Z])(?:ACH|NACH|ENACH|ECS|CHQ|CHEQUE|CHEQ|MANDATE|EMI|SI|I/W|INWARD|O/W|OUTWARD|CLG|"
                     r"CLEARING|AUTOPAY|ACHDR|NACHDR|ACH_DR)(?![A-Z])")
BOUNCE_CONTEXT = ["ACH", "NACH", "ECS", "CHQ", "CHEQUE", "CHEQ", "MANDATE", "EMI", "LOAN", " SI ", "SI-", "I/W",
                  "INWARD", "O/W", "OUTWARD", "CLG", "CLEARING", "AUTOPAY"]  # legacy list (not used for bounces)
# a debit that presents a mandate / EMI / cheque: only these can bounce (a failed UPI/IMPS is a reversal, not a bounce)
PRESENTMENT_RE = (r"(?<![A-Z])(?:ACH|NACH|ENACH|ECS|EMI|SI|CHQ|CHEQUE|MANDATE|I/W|CLG|ACHINWDR|ACHDR|NACHDR|ACH_DR)"
                  r"(?![A-Z])|ACHINWDR")
# person-to-person channels: a 'return' word here is the sender's remark ('UPI-..-RETURN VASU EMI', HDFC_005)
TRANSFER_CHANNEL_RE = r"^[-.\s]*(?:UPI|IMPS|NEFT|RTGS|MMT|REV-UPI|POS |NWD|ATW|ATM)"
# an outward transfer coming back ('RETURN IMPS-..', 'NEFT RETURN--..', 'IMPS REJECT|..', 'UPIRET-..'): not a bounce
TRANSFER_RETURN_RE = (r"^[-.\s]*(?:RETURN\s+(?:IMPS|NEFT|RTGS|UPI)|(?:NEFT|RTGS|IMPS|UPI)[\s\-/]*(?:RETURN|RTN|REJECT)"
                      r"(?![A-Z])|UPI\s?RET-)")
# the holder deposited a cheque and it came back unpaid (AU 'CHQDEP RET - FUNDS INSUFFICIENT'): the drawer bounced
DEPOSIT_RETURN_RE = r"CHQDEP RET"
CHARGE_WORDS = ["CHARGE", "CHRG", "CHGS", "CHG", " FEE", "FEES", "PENALTY", "PENAL", " GST", "SMS ALERT", "AMC",
                "MIN BAL", "MINIMUM BAL", "NON MAINT", "MAB ", "AMB ", "AVG BAL", "ANNUAL FEE", "DEBIT CARD FEE"]
CASH_WDL_WORDS = ["ATM", "NFS", "CASH WDL", "CASH WITHDRAWAL", "CWDR", "ATW", "CSH WDL", "SELF CASH", "CASH PAID",
                  "BY CASH WDL", "CASH-WDL", "ATM WDL"]
CASH_DEP_WORDS = ["CASH DEP", "CDM", "BY CASH", "CASH DEPOSIT", "CSH DEP", "CASH CR", "CASHDEP", "CASH-DEP", "BNA",
                  "CASH RECEIVED"]
INTEREST_WORDS = ["INT.PD", "INT PD", "INTEREST", "INT CR", "SB INT", "INT.CR", "INT CREDIT", "FD INT"]  # 'FD INT PAYOUT'
REVERSAL_WORDS = ["REVERSAL", "REFUND", "REV ", "RVSL", "CASHBACK", "CASH BACK", "RETURNED", "RFND"]
INSURANCE_WORDS = ["LIC OF INDIA", "LIFE INSURANCE", "INSURANCE", "POLICYBAZAAR", "HDFC LIFE", "ICICI PRU",
                   "SBI LIFE", "MAX LIFE", "TATA AIA", "BAJAJ ALLIANZ", "STAR HEALTH", "ACKO", "DIGIT INSURANCE"]
INVESTMENT_WORDS = ["ZERODHA", "GROWW", "UPSTOX", "ANGEL ONE", "ANGELONE", "MUTUAL FUND", " SIP", "SIP ", "MF ",
                    "NSE CLEARING", "BSE ", "INDMONEY", "KUVERA", "PAYTM MONEY", "ICICI SEC", "HDFC SEC", "PPF", "NPS",
                    # MF clearing / registrars collecting SIPs ('BDECS-ICCL-', 'UPI-ICCL-ICCLMF@YBL'). Spelled out:
                    # a bare 'KFIN' hits 'KONARKFINSERV', and 'CAMSPAY' is an NBFC EMI gateway (Mahindra Rural Housing)
                    "ICCL", "INDIAN CLEARING", "BSE STAR", "CAMS ONLINE", "CAMSONLINE", "KFINTECH", "KFIN TECH"]
UTILITY_WORDS = ["ELECTRICITY", "BESCOM", "MSEDCL", "TATA POWER", "ADANI ELEC", "BSES", "TORRENT POWER", "WATER",
                 "GAS ", "IGL", "MGL", "BROADBAND", "AIRTEL", "JIO", "VODAFONE", "VI ", "BSNL", "DTH", "TATA PLAY",
                 "RECHARGE", "BILL PAY", "BBPS"]
RENT_WORDS = ["RENT", "NOBROKER", "HOUSING.COM", "NESTAWAY"]


# ---------------------------------------------------------------- word-bounded category patterns (audit 2026-11-13)
# The plain lists above match as substrings, and short keywords hit inside names / refs / handles: 'ATM' in Indian
# Bank's '/BRANCH : ATM SERVICE BRANCH' (every row), 'VI ' in 'RAVI ', 'AMC' in 'RAMCHANDRA', 'CHARGE' in 'RECHARGE',
# 'NPS' in a BillDesk ref '..3ONPSGV..', 'MPL ' in 'SIMPL ', 'BNA' in 'DEBNATH', 'POS' in 'IPOS0000001' / 'POSTPAID'.
# categorize() uses these instead. _LB = no letter before (keyword starts a word), _RB = no letter after.
_LB, _RB = r"(?<![A-Z])", r"(?![A-Z])"


def _kw_re(words: list[str], both: set[str] = frozenset(), left: set[str] = frozenset(),
           drop: set[str] = frozenset()) -> re.Pattern:
    parts = []
    for w in words:
        k = w.strip()
        if k in drop:
            continue
        if k in both:  # a list word with a trailing space keeps needing that space ('MAB ' is not 'MAB.0373..@AXIS')
            parts.append(_LB + re.escape(k) + (" " if w.endswith(" ") else _RB))
        elif k in left:
            parts.append(_LB + re.escape(k))
        else:
            parts.append(re.escape(w))
    return re.compile("|".join(parts))


# Cash withdrawal forms seen in the samples: 'ATW-4059..', 'ATW/1182/', 'NFS/CASH', 'NFS-CWDR/', 'BNA-CWDR/',
# 'ATM/CASH/', 'ATM WDL-ATM CASH', 'ATM-NFS/CASH WITHDRAWAL', 'ICICI ATM ATM/..', 'DCBCWD ATM', 'ATM-IOB ..ONSITE',
# 'UPI/DR/../ATMWDL/' (UPI cash at ATM), '../ATMISS/<place>', 'CSHWDL 051 SELF', 'CASH WITHDRAWAL-', 'CASH PAID:',
# Indian Bank card rows 'TRAN TIME -(HHMMSS) 193108/SELF-<ATM place>'. 'ATM' never counts in a branch label
# ('BRANCH : ATM SERVICE BRANCH'), a clearing label ('AXIS BK ATM CLEARING') or a card fee ('ATM ANNUAL CHRG').
CASH_WDL_RE = re.compile(
    _LB + r"(?:ATW|CWDR|NFS|ATMWDL|ATMISS|CSHWDL|DCBCWD)" + _RB
    + r"|(?<!BRANCH : )(?<!BRANCH :)" + _LB + r"ATM" + _RB + r"(?!\s*(?:SERVICE|CLEARING|ANNUAL))"
    + r"|CASH[\s\-]?(?:WDL|WITHDRAWAL|PAID)|CSH WDL|SELF CASH|HHMMSS\)\s*\d{6}/SELF-")
CASH_DEP_RE = _kw_re(CASH_DEP_WORDS, both={"BNA", "CDM"}, drop={"BY CASH"})
CASH_DEP_RE = re.compile(CASH_DEP_RE.pattern + r"|BY CASH" + _RB)  # not 'BY CASHFREE'
# glued charge codes are real ('INSTAALERTCHG', 'ECSRTNCHGS', 'FUEL SURCHG'), so CHG/CHGS/CHRG stay substrings;
# 'CHARGE' must not be 'RECHARGE' / 'FREECHARGE' (wallet handle); short words must be whole words ('MUFEESH',
# 'RAMCHANDRA', 'devanand ramb', 'RAVIVARMAB')
CHARGE_RE = _kw_re(CHARGE_WORDS, both={"AMC", "MAB", "AMB"}, drop={"CHARGE", "FEE", "FEES"})
# FEE also never next to a digit: UPI hex ids carry it ('..ICI9cc72fb03b9 f48ccbe292fee', 'ICI75fee6649..')
CHARGE_RE = re.compile(CHARGE_RE.pattern + r"|(?<!RE)(?<!FREE)CHARGE|(?<![A-Z0-9])FEES?(?![A-Z0-9])")
# 'VI' (Vodafone Idea) only as the payee field itself ('UPI/DR/../VI S18729949', 'DR/VI/PYT', 'UPI-VI-VILPRECHN',
# '-VI.BDPG@', HDFC SI billpay 'SI HGALP..5586 VI 0000', 'VIPOSTPAIDF-BILLPAY'), never a name part ('SUNANDA VI',
# 'RAGHA VI FINANCE', 'HOTEL VI'); AIRTEL not as a PSP handle ('..@MAIRTEL', '..RZP@RXAIRTEL') or the payee's bank
# ('/AIRTEL PAYMENTS/'), but glued brand names count ('PAYUBHARTIAIRTELLIMI'); JIO not as Jio Payments Bank ('/JIOP/',
# 'JIOP0000001'); GAS not a ref fragment 'IGAS' ('..3589672279IGAS', 'IGAS UHREO7'; 'CCAINDANEGAS' counts); IGL a
# whole word ('CAPRIGLOBAL'); DTH/MGL/WATER start a word ('PZDTH')
UTILITY_RE = _kw_re(UTILITY_WORDS, both={"IGL"}, left={"DTH", "MGL", "WATER"}, drop={"VI", "AIRTEL", "JIO", "GAS"})
UTILITY_RE = re.compile(UTILITY_RE.pattern + r"|[/\-]VI(?=[/\-\s.])|\d{6,} VI \d|VI ?(?:POSTPAID|PREPAID)"
                        + r"|(?<!@)(?<!M)(?<!RX)AIRTEL(?! PAYMENTS)|" + _LB + r"JIO(?!P(?![A-Z]))|(?<!I)GAS" + _RB)
RENT_RE = _kw_re(RENT_WORDS, left={"RENT"})  # not 'TRENT HYPERMARKET' / 'TRENTWESTSIDE' / 'CURRENT'
INVESTMENT_RE = _kw_re(INVESTMENT_WORDS, both={"SIP", "BSE", "PPF"}, left={"NPS"}, drop={"MF"})  # 'ARIHANT SIPANI', 'PIRAMALCAPPF'
INVESTMENT_RE = re.compile(INVESTMENT_RE.pattern + "|" + _LB + "MF" + _RB)  # 'O-MF-HDFC-MCOG' (HDFC MF SIP)
GAMBLING_RE = _kw_re(GAMBLING_WORDS, both={"MPL"}, drop={"A23"})  # 'MPL ' hit 'SIMPL BILL', 'DREA MPLUG'
GAMBLING_RE = re.compile(GAMBLING_RE.pattern + r"|(?<![A-Z0-9])A23(?![A-Z0-9])")  # not a hex ref '..83901da23'
# channel words: 'UPI' starts a word or ends a UPI pseudo-IFSC ('YESB0YBLUPI/', 'HDFC0MERUPI'), never 'JUPITER' /
# 'BAHURUPI'; 'POS' is a whole word, never 'IPOS0000001' (India Post IFSC), 'POSTPAID', 'BULK POSTING'
UPI_RE = re.compile(_LB + r"UPI|\b[A-Z]{4}0[A-Z0-9]{2,6}UPI")
POS_RE = re.compile(_LB + r"POS" + _RB)
# an outward transfer that failed and came back: 'IMPS REJECT|P2A|..|UNABLE TO PROCESS ..' (SCB_001)
FAILED_TRANSFER_RE = re.compile(r"\b(?:IMPS|NEFT|RTGS|UPI)\s*REJECT\b|\bUNABLE TO PROCESS\b")
# loan payout wording on a credit ('LOAN DISBURSAL', 'LOANDISBURSA', 'PAYMENTDISBU', 'AMOUNT DISBURSED',
# 'TREASURYDISBURS', 'DISBLPPLH', 'PL STF DISB') - every 'DISB' credit in the 97 samples is a loan payout
DISBURSAL_TEXT_RE = re.compile(r"DISB")
