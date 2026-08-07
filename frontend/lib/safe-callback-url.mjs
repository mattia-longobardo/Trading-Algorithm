// Base fittizia usata solo per normalizzare il path: se dopo la risoluzione
// l'origin cambia, il valore non era un path same-origin.
const RELATIVE_BASE = "http://callback.invalid";

/**
 * Accetta solo path relativi same-origin ("/rotta"), altrimenti "/".
 *
 * Il parser WHATWG normalizza backslash, caratteri di controllo e "//host",
 * quindi qualsiasi tentativo di uscire dal dominio cambia l'origin e viene
 * scartato. Va però controllato anche l'OUTPUT: la risoluzione dei segmenti
 * `..`/`.` può risalire oltre la radice e produrre un path che ricomincia con
 * "//" (es. "/..//evil.example" → "//evil.example"), cioè un URL
 * protocol-relative che `redirect()` scriverebbe verbatim in Location.
 *
 * Modulo .mjs (non .ts) così che il check accanto giri con node puro.
 *
 * Il controllo di tipo è obbligatorio e sta fuori dal try: con il parametro
 * ripetuto (?callbackUrl=/a&callbackUrl=/b) Next passa un array, e chiamare
 * .startsWith su un array solleverebbe prima ancora di entrare nel try → 500
 * sulla pagina di login.
 *
 * @param {string | string[] | undefined} raw valore grezzo di ?callbackUrl
 * @returns {string} path relativo sicuro, oppure "/"
 */
export function safeCallbackUrl(raw) {
  if (typeof raw !== "string" || !raw.startsWith("/")) return "/";
  try {
    const url = new URL(raw, RELATIVE_BASE);
    if (url.origin !== RELATIVE_BASE) return "/";
    const path = `${url.pathname}${url.search}${url.hash}`;
    return path.startsWith("//") ? "/" : path;
  } catch {
    return "/";
  }
}
