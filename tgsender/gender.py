"""Best-effort gender guess from a Telegram display name.

Telegram has no gender field, so this is a heuristic over whatever people typed
into their profile. It is tuned for Russian and Kazakh names plus common Latin
spellings, and it prefers answering "unknown" to guessing when a name is
genuinely ambiguous (Саша, Женя, Валя).

Order of evidence, strongest first:
  1. a known first name anywhere in the display name
  2. a gendered surname or patronymic ending (-ова / -ов, -ская / -ский, -қызы / -ұлы)
  3. the ending of the first word (-а/-я → female, consonant → male), skipping
     known male names that end in -а/-я (Никита, Илья, Миша)

Returns "f", "m" or "u".
"""

from __future__ import annotations

import re

FEMALE, MALE, UNKNOWN = "f", "m", "u"


def _words(text: str) -> set[str]:
    return {w.strip().lower().replace("ё", "е") for w in text.split() if w.strip()}


FEMALE_NAMES = _words("""
александра алена алина алиса алла анастасия анжелика анна антонина ангелина
арина валентина валерия варвара василиса вера вероника виктория виолетта вита
влада владислава галина дарина дарья диана дина ева евгения екатерина елена
елизавета есения жанна зинаида злата зоя инна ирина камилла камила карина кира
клавдия кристина ксения лариса лидия лилия любовь людмила майя маргарита марина
мария марьяна милана мила мирослава надежда наталья наталия нина оксана олеся
ольга полина раиса регина римма светлана серафима снежана софия софья
станислава стефания таисия тамара татьяна ульяна фаина эвелина элина эльвира
эльмира эмилия элеонора эмма юлиана юлия яна ярослава изабелла сабрина самира
аня анечка маша машенька катя катюша лена леночка оля оленька ира иришка
наташа таня танюша света юля юленька настя настенька даша дашенька ксюша лиза
вика соня поля галя люда люба надя тоня рита лера аленка варя уля алёнка
кристя нюра дуся лиля милка
алия амина аиша ляйсан резеда розалия лейла лейля диляра гузель гульнара
гульшат алсу айгуль айгерим айжан асель асем аружан ажар акбота аяулым
балжан гаухар данара динара жанар жансая жулдыз зарина индира карлыгаш
куралай мадина меруерт назира назым салтанат сабина сауле томирис улжан
умида фарида шолпан эльнара дильназ дильнара айсулу айдана жибек жазира
анель аминат патимат хадижат зульфия гульмира гульнур нургуль айнур
anna ann anne anya maria mariya mary masha olga olya elena lena helen natalia
natalya natasha tatiana tatyana tanya ekaterina katerina katya kate kathy irina
ira svetlana sveta julia yulia yuliya julie anastasia nastya daria darya dasha
ksenia kseniya ksusha alina alena alyona polina sofia sofiya sophia sophie sonya
victoria viktoria vika kristina christina valeria lera marina margarita rita
elizaveta liza lisa diana veronika veronica yana jana karina milana kira eva
arina angelina evgenia evgeniya galina lyudmila lyuda lubov lyubov nadezhda
nadya vera nina oksana olesya inna zarina madina aigerim aizhan asel dinara
aliya amina gulnara kamila camila emily emma sabina saule zhanna alice alisa
nicole sarah laura jessica jennifer amanda linda susan lucy mia chloe
aruzhan akbota ayaulym balzhan gaukhar zhuldyz indira meruert saltanat sholpan
leyla leila dilyara guzel alsu aigul valentina tamara taisia ulyana vasilisa
""")

MALE_NAMES = _words("""
александр алексей анатолий андрей антон аркадий арсений артем артур богдан
борис вадим валентин валерий василий виктор виталий владимир владислав
всеволод вячеслав геннадий георгий глеб григорий давид даниил данил данила
денис дмитрий евгений егор иван игорь илья кирилл константин лев леонид
макар максим марк матвей михаил никита николай олег павел петр роман руслан
савелий савва святослав семен сергей станислав степан тимофей тимур федор
филипп эдуард юрий ян ярослав эмиль эрик остап назар кузьма фома лука
миша паша дима димон коля вася петя ваня леша лёша леха алеша сережа серега
гоша костя толя вова вовка володя стас влад вадик витя гена гриша жора
илюша кирюха макс никитос олежка рома ромка тема темка тимоха федя юра юрка
славик эдик андрюха лева левушка боря сеня степа митя димка санек саня
рустам ринат ренат марат айдар азат альберт ильдар ильнур ильяс камиль
рамиль тагир фарид эльдар эльвин самир амир али ахмед магомед мурат ислам
адиль айбек алмаз алихан арман асхат аскар бауыржан бауржан бекзат болат
галым даурен дамир данияр ербол ерлан ержан жандос жанибек канат мирас
нурлан нуржан нурсултан олжас рахат санжар серик талгат темирлан чингиз
шерхан ельдос берик асет алибек бахтияр досым самат мадияр еркебулан рауан
ануар габит куаныш мейрам нурбол аян азамат арсен мухтар ерасыл нуркен
бекжан абай абылай ерназар жасулан
alexander aleksandr alexandr alex alexey aleksey alexei andrey andrei andrew
anton artem artyom artur arthur boris vadim valentin valery valeriy vasily
victor viktor vitaly vitaliy vladimir vova vlad vladislav vyacheslav gennady
georgy george gleb grigory david daniil daniel danil denis dmitry dmitriy
dmitrii dmitri dima egor yegor evgeny evgeniy ivan igor ilya ilia kirill
konstantin kostya lev leonid maxim maksim max mark matvey mikhail michael
misha nikita nikolay nikolai nick oleg pavel paul pasha petr pyotr peter
roman roma ruslan semyon sergey sergei serega stanislav stas stepan timofey
timur fedor fyodor philip filipp eduard edward yuri yury yuriy yaroslav
john james tom alan eric erik emil arman aidar nurlan yerlan erlan askar
askhat damir daniyar kanat marat murat rustam rinat azamat baurzhan
bauyrzhan olzhas nursultan alibek arsen arseniy bogdan makar savely ilyas
ildar amir ali islam adil aibek dauren yerbol dauren sanzhar serik talgat
temirlan chingiz madiyar ansar mukhtar
""")

# Names used for both sexes: they never decide anything on their own, and they
# must not fall through to the -а/-я rule either (Саша is not a woman's name).
AMBIGUOUS_NAMES = _words("""
саша шура женя валя слава саня бахыт дидар жаксылык сакен
sasha zhenya valya slava bakhyt didar jenya shura andrea
""")

_FEMALE_SURNAME = re.compile(
    r"(ова|ева|ина|ына|ская|цкая|ая|овна|евна|ична|кызы|қызы|"
    r"ova|eva|ina|yna|skaya|tskaya|kaya|ovna|evna|kyzy)$"
)
_MALE_SURNAME = re.compile(
    r"(ов|ев|ин|ын|ский|цкий|ской|ой|ович|евич|улы|ұлы|"
    r"ov|ev|in|yn|sky|skiy|skii|tskiy|ovich|evich|uly)$"
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_LATIN = re.compile(r"^[a-z]+$")
_CYRILLIC_VOWEL_END = "аяоеиуюэыь"


def _tokens(*parts: str | None) -> list[str]:
    text = " ".join(p for p in parts if p)
    return [
        t.lower().replace("ё", "е").replace("і", "и").replace("ї", "и")
        for t in _WORD.findall(text)
    ]


def _by_surname(token: str) -> str:
    if len(token) < 4:
        return UNKNOWN
    if _FEMALE_SURNAME.search(token):
        return FEMALE
    if _MALE_SURNAME.search(token):
        return MALE
    return UNKNOWN


def _by_ending(token: str) -> str:
    """The weakest signal: the shape of a first name nobody listed."""
    if len(token) < 3 or token in AMBIGUOUS_NAMES:
        return UNKNOWN
    last = token[-1]
    if last in "ая" or (_LATIN.match(token) and last == "a"):
        return FEMALE
    if _LATIN.match(token):
        return MALE if last not in "aeiouy" else UNKNOWN
    if last not in _CYRILLIC_VOWEL_END:
        return MALE
    return UNKNOWN


def guess(first_name: str | None, last_name: str | None = None) -> str:
    tokens = _tokens(first_name, last_name)
    if not tokens:
        return UNKNOWN

    for token in tokens:
        if token in AMBIGUOUS_NAMES:
            continue
        if token in FEMALE_NAMES:
            return FEMALE
        if token in MALE_NAMES:
            return MALE

    # Surnames: prefer the separate last-name field, then any extra words in
    # the first-name field ("Анна Иванова" typed into one box).
    candidates = _tokens(last_name) + tokens[1:]
    for token in candidates:
        if token in AMBIGUOUS_NAMES or token in FEMALE_NAMES or token in MALE_NAMES:
            continue
        verdict = _by_surname(token)
        if verdict != UNKNOWN:
            return verdict

    return _by_ending(tokens[0])


LABELS = {FEMALE: "♀ женщины", MALE: "♂ мужчины", UNKNOWN: "❔ не определён"}
ICONS = {FEMALE: "♀", MALE: "♂", UNKNOWN: "❔"}
