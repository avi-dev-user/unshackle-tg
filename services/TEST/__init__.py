# ============================================================================
# TEST - שירות לימוד מינימלי. מקבל URL של manifest (DASH/HLS) ומוריד אותו.
# מיועד לתרגול על תוכן בדיקה ציבורי בלבד.
#   unshackle dl --list TEST "<url>"      ← רק להציג
#   unshackle dl TEST "<url>"             ← להוריד
# ============================================================================

import click                                             # ספריית ה-CLI - מגדירה ארגומנטים ואפשרויות

from unshackle.core.manifests import DASH, HLS           # ה-parsers שהופכים manifest ל-tracks
from unshackle.core.service import Service               # מחלקת הבסיס שכל שירות יורש ממנה
from unshackle.core.titles import Movie, Movies          # Movie = סרט בודד, Movies = הקונטיינר שלו
from unshackle.core.tracks import Chapters, Tracks       # Chapters = פרקי זמן, Tracks = אוסף המסלולים


class TEST(Service):                                     # מגדירים שירות בשם TEST שיורש מ-Service
    """Generic manifest tester - point it at a public DASH/HLS URL."""  # תיאור קצר של השירות

    ALIASES = ()                                         # כינויים נוספים ל-tag (ריק = רק "TEST" עובד)
    GEOFENCE = ()                                        # אזורים מותרים (ריק = ללא נעילה גיאוגרפית)

    # --- שכבת ה-CLI: מה שהמשתמש מקליד אחרי "dl TEST" ---
    @staticmethod                                        # אין self - נקרא לפני שקיים אובייקט TEST
    @click.command(name="TEST", short_help="Download from a raw DASH/HLS manifest URL (learning).")  # שם תת-הפקודה
    @click.argument("url", type=str)                     # ארגומנט חובה: כתובת ה-manifest
    @click.option("-t", "--title", default="Test Stream", help="Title name for the output file.")  # שם לקובץ הפלט
    @click.option("-y", "--year", default=2024, type=int, help="Year for the output file.")  # שנה לקובץ הפלט
    @click.option("-L", "--license", "license_url", default=None,  # כתובת שרת הרישיון (ל-DRM)
                  help="Widevine license server URL (for DRM test vectors).")
    @click.option("-H", "--lic-header", "lic_headers", multiple=True,  # header נוסף לבקשת הרישיון (אפשר כמה)
                  help="Extra license request header 'Name: Value' (e.g. Axinom X-AxDRM-Message).")
    @click.pass_context                                  # מזריק את ctx (ההקשר של click, מכיל את דגלי dl)
    def cli(ctx, **kwargs):                              # הפונקציה שמקבלת את כל מה שהוקלד
        return TEST(ctx, **kwargs)                       # יוצרת ומחזירה אובייקט TEST - הליבה ממשיכה מכאן

    # --- בנאי: שומרים את הקלט, ואז קוראים ל-super (שמקים session/proxy/cache) ---
    def __init__(self, ctx, url: str, title: str, year: int, license_url, lic_headers):  # מקבל את הארגומנטים מ-cli
        self.url = url                                   # שומרים את ה-URL לשימוש מאוחר יותר
        self.title_name = title                          # שומרים את שם הכותר
        self.year = year                                 # שומרים את השנה
        self.license_url = license_url                   # שומרים את כתובת שרת הרישיון
        # ממירים את ה-headers ("Name: Value") ל-dict, אם ניתנו:
        self.lic_headers = dict(h.split(":", 1) for h in lic_headers) if lic_headers else {}  # פיצול לפי ":"
        self.lic_headers = {k.strip(): v.strip() for k, v in self.lic_headers.items()}  # ניקוי רווחים
        super().__init__(ctx)                            # חובה! מקים session, proxy, geofence, cache

    # --- מה מורידים: עוטפים את ה-manifest ב-Movie אחד ---
    def get_titles(self) -> Movies:                      # מחזיר את רשימת הכותרים (כאן: סרט אחד)
        return Movies([                                  # Movies = הקונטיינר; חייב להחזיר קונטיינר ולא אובייקט בודד
            Movie(                                       # יוצרים Movie אחד
                id_=self.url,                            # מזהה ייחודי (כאן ה-URL עצמו)
                service=self.__class__,                  # הפניה למחלקת השירות (TEST)
                name=self.title_name,                    # שם → נכנס ל-{title} בשם הקובץ
                year=self.year,                          # שנה → {year}
                data={"url": self.url},                  # "כיס" חופשי: שומרים מידע ל-get_tracks
            )
        ])

    # --- המסלולים: בוחרים parser לפי סיומת, והוא בונה את ה-tracks ---
    def get_tracks(self, title) -> Tracks:               # מקבל את ה-Movie, מחזיר את המסלולים שלו
        url = title.data["url"]                          # שולפים את ה-URL מה"כיס" ששמרנו
        if ".m3u8" in url.lower():                       # אם זה HLS...
            manifest = HLS.from_url(url, self.session)    # ...משתמשים ב-HLS parser
        else:                                            # אחרת...
            manifest = DASH.from_url(url, self.session)   # ...ב-DASH parser
        return manifest.to_tracks(language="en")          # to_tracks בונה Video/Audio/Subtitle. בלי סינון!

    # --- צ'פטרים: למניפסט גולמי אין, אז מחזירים ריק ---
    def get_chapters(self, title) -> Chapters:           # מקבל את ה-Movie, מחזיר פרקי זמן
        return Chapters()                                # ריק - לגיטימי לחלוטין

    # --- DRM: שולחים את ה-challenge (שהליבה בנתה עם ה-CDM) לשרת הרישיון ---
    def get_widevine_license(self, *, challenge, title, track):  # challenge = bytes שה-CDM ייצר
        if not self.license_url:                         # אם לא ניתנה כתובת שרת רישיון...
            raise ValueError("No license URL provided. Pass -L <license_server_url>.")  # ...שגיאה ברורה
        return self.session.post(                        # שולחים POST לשרת הרישיון
            self.license_url,                            # הכתובת שניתנה ב--L
            data=challenge,                              # ה-challenge הגולמי (bytes - בלי base64!)
            headers=self.lic_headers,                    # headers נוספים (למשל ה-token של Axinom)
        ).content                                        # מחזירים את התשובה הגולמית (bytes) לליבה
