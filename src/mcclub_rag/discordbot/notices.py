"""Notices the bot writes itself, in the language of the question (Req 7.2).

Only the failure modes with no API reply live here. Everything a member normally reads — the
answer, the refusal, the passages — comes from the API, which already writes it in the right
language, so the bot never restates it.
"""

from typing import Literal

from mcclub_rag.text.lang import detect_language

NoticeLanguage = Literal["ar", "fr", "en"]

NOT_GROUNDED: dict[str, str] = {
    "en": "Not grounded in the documents.",
    "fr": "Non fondé sur les documents.",
    "ar": "غير مستند إلى الوثائق.",
}
TOO_LONG: dict[str, str] = {
    "en": "That question is longer than 2000 characters. Please shorten it.",
    "fr": "Cette question dépasse 2000 caractères. Merci de la raccourcir.",
    "ar": "هذا السؤال يتجاوز 2000 حرف. الرجاء تقصيره.",
}
COOLDOWN: dict[str, str] = {
    "en": "Please wait {seconds} s before asking again.",
    "fr": "Veuillez attendre {seconds} s avant de poser une autre question.",
    "ar": "الرجاء الانتظار {seconds} ثانية قبل طرح سؤال آخر.",
}
BUSY: dict[str, str] = {
    "en": "The bot is answering other questions right now. Please try again shortly.",
    "fr": "Le bot répond à d'autres questions. Veuillez réessayer bientôt.",
    "ar": "البوت يجيب على أسئلة أخرى الآن. الرجاء المحاولة بعد قليل.",
}
UNAUTHORIZED: dict[str, str] = {
    "en": "That needs members access. Please ask an admin for the member link.",
    "fr": "Cela nécessite un accès membre. Demandez le lien membre à un admin.",
    "ar": "هذا يتطلب صلاحية الأعضاء. الرجاء طلب رابط العضوية من المشرف.",
}
RATE_LIMITED: dict[str, str] = {
    "en": "The bot has hit its request limit. Please try again shortly.",
    "fr": "Le bot a atteint sa limite de requêtes. Veuillez réessayer bientôt.",
    "ar": "وصل البوت إلى حد الطلبات. الرجاء المحاولة بعد قليل.",
}
SERVICE_UNAVAILABLE: dict[str, str] = {
    "en": "The answering service is not available right now. Please try again later.",
    "fr": "Le service de réponse n'est pas disponible pour le moment. Réessayez plus tard.",
    "ar": "خدمة الإجابة غير متاحة الآن. الرجاء المحاولة لاحقا.",
}
ANSWER_UNDELIVERED: dict[str, str] = {
    "en": ("I could not deliver that answer. The question took too long; please ask it again."),
    "fr": "Je n'ai pas pu livrer cette réponse. La question a pris trop de temps ; reposez-la.",
    "ar": "لم أتمكن من تسليم هذه الإجابة. استغرق السؤال وقتا طويلا؛ الرجاء طرحه مرة أخرى.",
}
FEEDBACK_RECORDED: dict[str, str] = {
    "en": "Thanks, that rating was recorded.",
    "fr": "Merci, votre avis a été enregistré.",
    "ar": "شكرا، تم تسجيل تقييمك.",
}
FEEDBACK_FAILED: dict[str, str] = {
    "en": "I could not record that rating.",
    "fr": "Je n'ai pas pu enregistrer cet avis.",
    "ar": "لم أتمكن من تسجيل هذا التقييم.",
}
RESET_DONE: dict[str, str] = {
    "en": "Started a new conversation in this channel.",
    "fr": "Nouvelle conversation démarrée dans ce salon.",
    "ar": "تم بدء محادثة جديدة في هذه القناة.",
}
STATUS: dict[str, str] = {
    "en": (
        "API: {status} · documents: {documents} · "
        "cooldown: {cooldown}s · concurrency: {concurrency}"
    ),
    "fr": (
        "API : {status} · documents : {documents} · "
        "délai : {cooldown}s · concurrence : {concurrency}"
    ),
    "ar": (
        "الواجهة: {status} · الوثائق: {documents} · الانتظار: {cooldown}ث · التزامن: {concurrency}"
    ),
}


ADMIN_ONLY: dict[str, str] = {
    "en": "That command is for club admins only.",
    "fr": "Cette commande est réservée aux admins du club.",
    "ar": "هذا الأمر مخصص لمشرفي النادي فقط.",
}
ADMIN_REFUSED: dict[str, str] = {
    "en": (
        "The API refused the admin key. Check ADMIN_API_KEY on the app and on the bot, "
        "then try again."
    ),
    "fr": (
        "L'API a refusé la clé admin. Vérifiez ADMIN_API_KEY côté app et côté bot, puis réessayez."
    ),
    "ar": "رفضت الواجهة مفتاح المشرف. تحقق من ADMIN_API_KEY في التطبيق وفي البوت ثم أعد المحاولة.",
}
ONE_SOURCE: dict[str, str] = {
    "en": "Give either a file or a url — not both, and not neither.",
    "fr": "Donnez soit un fichier, soit une url — pas les deux, et pas aucun.",
    "ar": "أعطني ملفا أو رابطا — لا كليهما ولا أي منهما.",
}
INGEST_STARTED: dict[str, str] = {
    "en": "Ingesting {name} as {visibility}. This can take minutes; I will report here.",
    "fr": "Ingestion de {name} en {visibility}. Cela peut prendre des minutes ; je répondrai ici.",
    "ar": "جاري إدخال {name} بصلاحية {visibility}. قد يستغرق دقائق؛ سأخبرك هنا.",
}
# The API answers `{doc_id}` and nothing else, so the bot cannot say indexed vs replaced vs
# duplicate here: it reports what it actually knows (EDGE_CASES.md).
INGEST_DONE: dict[str, str] = {
    "en": "{name} is in the corpus as {visibility}\n`{doc_id}`",
    "fr": "{name} est dans le corpus en {visibility}\n`{doc_id}`",
    "ar": "{name} أصبح في المجموعة بصلاحية {visibility}\n`{doc_id}`",
}
INGEST_FAILED: dict[str, str] = {
    "en": "Could not ingest {name}: {reason}",
    "fr": "Impossible d'ingérer {name} : {reason}",
    "ar": "تعذر إدخال {name}: {reason}",
}
NO_DOCUMENTS: dict[str, str] = {
    "en": "The corpus is empty: nothing has been ingested yet.",
    "fr": "Le corpus est vide : rien n'a encore été ingéré.",
    "ar": "المجموعة فارغة: لم يتم إدخال أي وثيقة بعد.",
}
DELETE_CONFIRM: dict[str, str] = {
    "en": "Delete {name} from the corpus? This cannot be undone.\n`{doc_id}`",
    "fr": "Supprimer {name} du corpus ? C'est irréversible.\n`{doc_id}`",
    "ar": "حذف {name} من المجموعة؟ لا يمكن التراجع.\n`{doc_id}`",
}
DELETE_DONE: dict[str, str] = {
    "en": "Removed from the corpus\n`{doc_id}`",
    "fr": "Supprimé du corpus\n`{doc_id}`",
    "ar": "تم الحذف من المجموعة\n`{doc_id}`",
}
DELETE_NOT_FOUND: dict[str, str] = {
    "en": "No document with that id — nothing was deleted.\n`{doc_id}`",
    "fr": "Aucun document avec cet identifiant — rien n'a été supprimé.\n`{doc_id}`",
    "ar": "لا توجد وثيقة بهذا المعرف — لم يتم حذف أي شيء.\n`{doc_id}`",
}
DELETE_CANCELLED: dict[str, str] = {
    "en": "Nothing was deleted.",
    "fr": "Rien n'a été supprimé.",
    "ar": "لم يتم حذف أي شيء.",
}


def notice_language(text: str) -> NoticeLanguage:
    """The language a notice about ``text`` is written in; English when it cannot be told."""
    language = detect_language(text)
    return language if language in ("ar", "fr", "en") else "en"


def notice(table: dict[str, str], language: NoticeLanguage, **values: object) -> str:
    """One notice, falling back to English for a language the table does not carry."""
    template = table.get(language) or table["en"]
    return template.format(**values) if values else template
