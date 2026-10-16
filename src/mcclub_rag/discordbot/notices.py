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


def notice_language(text: str) -> NoticeLanguage:
    """The language a notice about ``text`` is written in; English when it cannot be told."""
    language = detect_language(text)
    return language if language in ("ar", "fr", "en") else "en"


def notice(table: dict[str, str], language: NoticeLanguage, **values: object) -> str:
    """One notice, falling back to English for a language the table does not carry."""
    template = table.get(language) or table["en"]
    return template.format(**values) if values else template
