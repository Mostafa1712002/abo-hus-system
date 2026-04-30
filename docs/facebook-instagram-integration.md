# تكامل Facebook + Instagram

## الهدف

نشر نفس المحتوى اللي بيتنشر على YouTube على Facebook Page و Instagram تلقائياً، بنفس الـ metadata والتوقيت.

## نطاق النشر (مُتفق عليه مع المستخدم)

| المحتوى | YouTube | Facebook Page | Instagram |
|---|---|---|---|
| **الفيديو الكامل** (~90 دقيقة) | ✅ Video | ✅ Video Post | ❌ (طويل جداً) |
| **Shorts** (30-60 ثانية، 9:16) | ✅ Short | ✅ Reel | ✅ Reel |

## الـ Stack المطلوب

### من جهة Meta
- **Facebook Page** (مش حساب شخصي) — المستخدم لديه ✅
- **Instagram Business / Creator Account** مربوط بالـ Page — المستخدم لديه ✅
- **Meta Developer App** (نفس الـ App بيخدم FB + IG عبر Graph API)
- **Page Access Token** (Long-Lived — صلاحيته 60 يوم، فيه refresh logic)

### Permissions المطلوبة في الـ App
```
pages_show_list
pages_read_engagement
pages_manage_posts
pages_manage_metadata
publish_video                  (لرفع الفيديو على Page)
instagram_basic
instagram_content_publish      (لنشر Reels على IG)
business_management
```

## الخطوات المطلوبة من المستخدم (~15 دقيقة)

1. روح [developers.facebook.com/apps](https://developers.facebook.com/apps/) → **Create App** → نوع: "Business"
2. اسم الـ App: `Sami Al-Arabi Uploader` (أو أي اسم)
3. أضف منتجين: **Facebook Login for Business** و **Instagram Graph API**
4. روح **App Settings → Basic** → خد:
   - `App ID`
   - `App Secret`
5. روح [Graph API Explorer](https://developers.facebook.com/tools/explorer/) → Generate User Access Token مع كل الـ permissions أعلاه → اضغط Generate Token
6. روح [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/) → الصق الـ token → اضغط **Extend Access Token** لتحويله لـ Long-Lived (60 يوم)
7. اعمل API call عشان تحول من User Token لـ Page Token:
   ```
   GET https://graph.facebook.com/v21.0/me/accounts?access_token=<long-lived-user-token>
   ```
   هتلاقي `data[0].access_token` — ده Long-Lived Page Token (لا تنتهي صلاحيته)
8. خد **Page ID** من نفس الـ response (`data[0].id`)
9. خد **Instagram Business Account ID**:
   ```
   GET https://graph.facebook.com/v21.0/<page-id>?fields=instagram_business_account&access_token=<page-token>
   ```

## الـ Credentials اللي محتاج تبعت

```yaml
facebook:
  app_id: ...
  app_secret: ...
  page_id: ...
  page_access_token: ...    # long-lived, doesn't expire
  
instagram:
  business_account_id: ...
  # يستخدم نفس page_access_token
```

## البنية المقترحة في الكود

ملفات جديدة:
- `src/facebook_uploader.py` — `upload_video_to_page()`, `upload_reel_to_page()`
- `src/instagram_uploader.py` — `upload_reel_to_instagram()` (two-step: container → publish)

تعديلات:
- `src/pipeline.py`:
  - في `_process_one`: بعد رفع الفيديو الكامل على YouTube، رفعه على FB Page برضو
  - في `_upload_shorts_for_video`: بعد رفع كل short على YouTube، رفعه على FB Reel + IG Reel
- `config.json`: قسم جديد `facebook` و `instagram`

## نقاط فنية مهمة

1. **Instagram Reels تحتاج URL عام** للفيديو. خياراتنا:
   - رفع الـ short على Cloudflare R2 / S3 أولاً (مؤقتاً)
   - استخدام Facebook Resumable Upload API (يحوّل الفيديو لـ session_id ثم نمرره لـ IG)
   - **الأفضل:** نستخدم الـ Facebook resumable upload — مجاني، وسهل
2. **Token Expiry**: Long-Lived Page Token لا تنتهي، لكن لو تم إلغاء الصلاحيات يدوياً يفشل. نضيف helper لكشف الـ 401 وإشعار المستخدم.
3. **Rate Limits**:
   - Facebook: 200 calls/hour/user — كافي
   - Instagram: 25 published posts/day — كافي
4. **Aspect Ratio**: الـ shorts بتاعتنا 9:16 ✅ تشتغل مع IG Reels و FB Reels.
5. **Captions / Hashtags**: نفس الـ description الموجود في YouTube بيتكتب على FB/IG. الـ tags في FB بـ كلمات منفصلة (لا يوجد tags field رسمي زي YouTube، فبنحطها كـ #hashtags في الـ caption).

## الـ Error Handling

كل منصة في try/except منفصل. لو فشلت IG، الفيديو لسه على YouTube + FB (لا تتعطل سلسلة). الفشل بيتسجل في الـ pending tracker.

## الـ Status بعد التنفيذ

- [ ] User: إنشاء Meta App + توليد credentials
- [ ] User: إرسال الـ credentials
- [ ] Code: `src/facebook_uploader.py`
- [ ] Code: `src/instagram_uploader.py`
- [ ] Code: تكامل في `pipeline.py`
- [ ] Code: `config.json` schema
- [ ] Test: فيديو كامل + 3 shorts على فيديو تجريبي

## مصادر

- [Facebook Graph API — Video Upload](https://developers.facebook.com/docs/graph-api/reference/video)
- [Instagram Reels Publishing](https://developers.facebook.com/docs/instagram-api/guides/content-publishing#reels)
- [Long-Lived Tokens](https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived/)
