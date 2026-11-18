"""Render user-facing replies from *verified* step state (never from model prose).

Returns plain text plus a structured `actions` list (approval buttons etc.) that each channel
adapter renders natively (WhatsApp reply buttons, Telegram inline keyboard).
"""

from __future__ import annotations

from typing import Any

# Phrase table: en, hi (Devanagari), hi-Latn (Hinglish), mr (Marathi Devanagari).
_T: dict[str, dict[str, str]] = {
    "reminder_set": {
        "en": "⏰ Reminder set for {when}: {text}",
        "hi-Latn": "⏰ Reminder laga diya {when} ke liye: {text}",
        "hi": "⏰ {when} के लिए रिमाइंडर लगा दिया: {text}",
        "mr": "⏰ {when} साठी रिमाइंडर लावला: {text}",
    },
    "draft_ready": {
        "en": "✉️ Draft ready for {to}\nSubject: {subject}\n\n{body}",
        "hi-Latn": "✉️ {to} ke liye draft taiyaar hai\nSubject: {subject}\n\n{body}",
        "hi": "✉️ {to} के लिए ड्राफ्ट तैयार है\nSubject: {subject}\n\n{body}",
        "mr": "✉️ {to} साठी ड्राफ्ट तयार आहे\nSubject: {subject}\n\n{body}",
    },
    "approve_send": {
        "en": "Should I send this email to {to}? Nothing is sent until you approve.",
        "hi-Latn": "Kya main yeh email {to} ko bhej doon? Aapke approve karne tak kuch nahi bheja jayega.",
        "hi": "क्या मैं यह ईमेल {to} को भेज दूँ? आपकी मंज़ूरी तक कुछ नहीं भेजा जाएगा।",
        "mr": "हा ईमेल {to} ला पाठवू का? तुम्ही मंजूर करेपर्यंत काहीही पाठवले जाणार नाही.",
    },
    "approve_form": {
        "en": "Should I submit the form on {site} with: {fields}? Nothing is submitted until you approve.",
        "hi-Latn": "Kya main {site} par yeh form submit kar doon: {fields}?",
        "hi": "क्या मैं {site} पर यह फ़ॉर्म जमा कर दूँ: {fields}?",
        "mr": "{site} वर हा फॉर्म सबमिट करू का: {fields}?",
    },
    "sent_sandbox": {
        "en": "✅ Sandbox send recorded to {to} (id {id}). This is the mock mailbox - no real email was delivered.",
        "hi-Latn": "✅ Sandbox mein {to} ko send record hua (id {id}). Yeh mock mailbox hai - asli email nahi gaya.",
        "hi": "✅ सैंडबॉक्स में {to} को भेजना दर्ज हुआ (id {id})। यह मॉक मेलबॉक्स है - असली ईमेल नहीं गया।",
        "mr": "✅ सँडबॉक्समध्ये {to} ला पाठवणे नोंदले (id {id}). हा मॉक मेलबॉक्स आहे - खरा ईमेल गेला नाही.",
    },
    "sent_real": {
        "en": "✅ Email accepted by {provider} for {to} (id {id}).",
        "hi-Latn": "✅ {provider} ne {to} ke liye email accept kiya (id {id}).",
        "hi": "✅ {provider} ने {to} के लिए ईमेल स्वीकार किया (id {id})।",
        "mr": "✅ {provider} ने {to} साठी ईमेल स्वीकारला (id {id}).",
    },
    "not_sent": {
        "en": "Okay - not sent. The draft is kept.",
        "hi-Latn": "Theek hai - nahi bheja. Draft save hai.",
        "hi": "ठीक है - नहीं भेजा। ड्राफ्ट सुरक्षित है।",
        "mr": "ठीक आहे - पाठवले नाही. ड्राफ्ट जतन आहे.",
    },
    "trains_link": {
        "en": ("🚆 {o} → {d} on {date}{cls}{pref}.\nI don't have an authorised live-availability source yet, so I "
               "won't guess trains, fares or seats. Stations: {os} → {ds}.\nOfficial search: {link}"),
        "hi-Latn": ("🚆 {o} → {d}, {date}{cls}{pref}.\nAbhi koi authorised live-availability source connected nahi "
                    "hai, isliye main trains/fare/seats guess nahi karunga. Stations: {os} → {ds}.\n"
                    "Official search: {link}"),
        "hi": ("🚆 {o} → {d}, {date}{cls}{pref}।\nअभी कोई अधिकृत लाइव-उपलब्धता स्रोत जुड़ा नहीं है, इसलिए ट्रेन/किराया/"
               "सीट का अनुमान नहीं लगाऊँगा। स्टेशन: {os} → {ds}।\nआधिकारिक खोज: {link}"),
        "mr": ("🚆 {o} → {d}, {date}{cls}{pref}.\nअद्याप अधिकृत लाइव्ह उपलब्धता स्रोत जोडलेला नाही, त्यामुळे ट्रेन/भाडे/"
               "सीट अंदाजाने सांगणार नाही. स्टेशन: {os} → {ds}.\nअधिकृत शोध: {link}"),
    },
    "grocery_cart_head": {
        "en": "🧪 TEST order (sandbox: no real order, no real money)\n🛒 {store} · delivery in ~{eta} min{where}",
        "hi-Latn": "🧪 TEST order (sandbox: asli order ya paisa nahi)\n🛒 {store} · ~{eta} min mein delivery{where}",
    },
    "grocery_pay": {
        "en": "💳 Pay {total} (TEST payment, nothing is charged) to place this order?",
        "hi-Latn": "💳 Yeh order place karne ke liye {total} pay karun (TEST payment, koi paisa nahi katega)?",
    },
    "grocery_placed": {
        "en": ("✅ TEST order placed · ID {code}\n🛒 {store} · arriving in ~{eta} min (simulated)\n"
               "💰 Paid {total} (test payment {ref}; nothing charged)\nThis is a sandbox order: nothing will be "
               "delivered. To order for real, use the {store} app."),
        "hi-Latn": ("✅ TEST order place hua · ID {code}\n🛒 {store} · ~{eta} min mein (simulated)\n"
                    "💰 {total} paid (test payment {ref}; koi paisa nahi kata)\nYeh sandbox order hai: kuch deliver "
                    "nahi hoga. Asli order ke liye {store} app use kijiye."),
    },
    "grocery_nothing": {
        "en": "🧪 I couldn't find {items} in the sandbox catalogue. Try common names like milk, atta, paneer or eggs.",
        "hi-Latn": "🧪 Sandbox catalogue mein {items} nahi mila. Doodh, atta, paneer, ande jaise naam try kijiye.",
    },
    "cinema_held": {
        "en": ("🧪 TEST booking (sandbox cinema: no real ticket, no real money)\n🎬 {movie} · {theatre}\n"
               "📅 {when}\n💺 {seats} · {cls} · {price} each{note}\n💰 Total {total} · seats held for {mins} min"),
        "hi-Latn": ("🧪 TEST booking (sandbox cinema: asli ticket ya paisa nahi)\n🎬 {movie} · {theatre}\n"
                    "📅 {when}\n💺 {seats} · {cls} · {price} each{note}\n💰 Total {total} · seats {mins} min ke liye hold"),
    },
    "cinema_pay": {
        "en": "💳 Pay {total} (TEST payment, nothing is charged) to confirm these seats?",
        "hi-Latn": "💳 In seats ke liye {total} pay karun (TEST payment, koi paisa nahi katega)?",
    },
    "cinema_booked": {
        "en": ("✅ TEST booking confirmed · ID {code}\n🎬 {movie} · {theatre} · {when}\n💺 {seats} ({cls})\n"
               "💰 Paid {total} (test payment {ref}; nothing charged)\nThis is a sandbox booking, not a real ticket. "
               "To book for real on BookMyShow: {link}"),
        "hi-Latn": ("✅ TEST booking confirm · ID {code}\n🎬 {movie} · {theatre} · {when}\n💺 {seats} ({cls})\n"
                    "💰 {total} paid (test payment {ref}; koi paisa nahi kata)\nYeh sandbox booking hai, asli ticket "
                    "nahi. Asli booking BookMyShow par: {link}"),
    },
    "cinema_not_listed": {
        "en": ("🎬 {movie} isn't in the sandbox cinema's listings (it knows movies BookMyShow lists, plus ones "
               "you send me a BookMyShow link for). BookMyShow: {link}"),
        "hi-Latn": ("🎬 {movie} sandbox cinema ki list mein nahi hai (BookMyShow wali movies, ya jinka link aap "
                    "bhejte hain). BookMyShow: {link}"),
    },
    "movie_show_page": {
        "en": ("🎬 {movie} - {city}, {day}{extra}\nShowtimes and seats on BookMyShow (you pick the show and pay "
               "there; I don't see prices or seats, so I won't guess them):\n{link}"),
        "hi-Latn": ("🎬 {movie} - {city}, {day}{extra}\nBookMyShow par showtimes aur seats (show aur payment aap "
                    "wahin karenge; mujhe price ya seats nahi dikhti, isliye guess nahi karunga):\n{link}"),
        "hi": ("🎬 {movie} - {city}, {day}{extra}\nBookMyShow पर शो-टाइम और सीटें (शो और पेमेंट आप वहीं करेंगे; "
               "मुझे कीमत या सीटें नहीं दिखतीं, इसलिए अनुमान नहीं लगाऊँगा):\n{link}"),
        "mr": ("🎬 {movie} - {city}, {day}{extra}\nBookMyShow वर शो-टाइम आणि सीट (शो आणि पेमेंट तुम्ही तिथेच "
               "कराल; मला किंमत किंवा सीट दिसत नाहीत, त्यामुळे अंदाज सांगणार नाही):\n{link}"),
    },
    "movie_unknown": {
        "en": ("🎬 {movie}{city}: I don't have this movie's BookMyShow ID yet, so here is the city page; pick "
               "the movie there:\n{link}\nSend me the movie's BookMyShow link once and next time I'll give "
               "you its showtimes page directly."),
        "hi-Latn": ("🎬 {movie}{city}: is movie ka BookMyShow ID abhi mere paas nahi hai, isliye city page de "
                    "raha hoon; wahan movie chun lijiye:\n{link}\nMovie ka BookMyShow link ek baar bhej dijiye, "
                    "agli baar seedha showtimes page dunga."),
        "hi": ("🎬 {movie}{city}: इस फ़िल्म का BookMyShow ID अभी मेरे पास नहीं है, इसलिए शहर का पेज दे रहा हूँ; "
               "वहाँ फ़िल्म चुनें:\n{link}\nफ़िल्म का BookMyShow लिंक एक बार भेज दें, अगली बार सीधा शो-टाइम पेज "
               "दूँगा।"),
        "mr": ("🎬 {movie}{city}: या चित्रपटाचा BookMyShow ID अजून माझ्याकडे नाही, म्हणून शहराचे पेज देतो; तिथे "
               "चित्रपट निवडा:\n{link}\nचित्रपटाची BookMyShow लिंक एकदा पाठवा, पुढच्या वेळी थेट शो-टाइम पेज "
               "देईन."),
    },
    "movie_link": {
        "en": ("🎬 {movie}{city}{extra}: book on BookMyShow (you choose the show and pay there; I can't see "
               "showtimes or prices, so I won't guess them):\n{link}"),
        "hi-Latn": ("🎬 {movie}{city}{extra}: BookMyShow par book kijiye (show aur payment wahin aap karenge; "
                    "mujhe showtimes ya price nahi dikhte, isliye guess nahi karunga):\n{link}"),
        "hi": ("🎬 {movie}{city}{extra}: BookMyShow पर बुक करें (शो और पेमेंट वहीं आप करेंगे; मुझे शो-टाइम या "
               "कीमत नहीं दिखती, इसलिए अनुमान नहीं लगाऊँगा):\n{link}"),
        "mr": ("🎬 {movie}{city}{extra}: BookMyShow वर बुक करा (शो आणि पेमेंट तिथेच तुम्ही कराल; मला शो-टाइम "
               "किंवा किंमत दिसत नाही, त्यामुळे अंदाज सांगणार नाही):\n{link}"),
    },
    "form_confirmed": {
        "en": "✅ Form submitted on {site}. Confirmation read from the page: {code}",
        "hi-Latn": "✅ {site} par form submit hua. Page par confirmation: {code}",
        "hi": "✅ {site} पर फ़ॉर्म जमा हुआ। पेज पर पुष्टि: {code}",
        "mr": "✅ {site} वर फॉर्म सबमिट झाला. पेजवरील पुष्टी: {code}",
    },
    "page_read": {
        "en": "📄 Read {site}: {summary}",
        "hi-Latn": "📄 {site} padha: {summary}",
        "hi": "📄 {site} पढ़ा: {summary}",
        "mr": "📄 {site} वाचले: {summary}",
    },
    "injection_note": {
        "en": "⚠️ That page contained instructions aimed at me. I ignored them - I only follow you.",
        "hi-Latn": "⚠️ Us page mein mere liye instructions likhe the. Maine ignore kiye - main sirf aapki baat maanta hoon.",
        "hi": "⚠️ उस पेज में मेरे लिए निर्देश लिखे थे। मैंने उन्हें अनदेखा किया - मैं सिर्फ़ आपकी बात मानता हूँ।",
        "mr": "⚠️ त्या पेजवर माझ्यासाठी सूचना होत्या. मी त्या दुर्लक्षित केल्या - मी फक्त तुमचे ऐकतो.",
    },
    "handoff": {
        "en": "🔐 {site} is asking for a password/OTP. I never handle those - please complete that step yourself.",
        "hi-Latn": "🔐 {site} password/OTP maang raha hai. Yeh main kabhi nahi bharta - yeh step aap khud kar dijiye.",
        "hi": "🔐 {site} पासवर्ड/OTP माँग रहा है। यह मैं कभी नहीं भरता - यह कदम आप खुद पूरा करें।",
        "mr": "🔐 {site} पासवर्ड/OTP मागत आहे. ते मी कधीच भरत नाही - ही पायरी तुम्ही स्वतः पूर्ण करा.",
    },
    "failed": {
        "en": "❌ Couldn't complete \"{what}\": {why}",
        "hi-Latn": "❌ \"{what}\" poora nahi ho paya: {why}",
        "hi": "❌ \"{what}\" पूरा नहीं हो पाया: {why}",
        "mr": "❌ \"{what}\" पूर्ण झाले नाही: {why}",
    },
    "skipped": {
        "en": "⏭️ Skipped \"{what}\" because an earlier step didn't finish.",
        "hi-Latn": "⏭️ \"{what}\" skip kiya kyunki pichla step poora nahi hua.",
        "hi": "⏭️ \"{what}\" छोड़ा क्योंकि पिछला कदम पूरा नहीं हुआ।",
        "mr": "⏭️ \"{what}\" वगळले कारण आधीची पायरी पूर्ण झाली नाही.",
    },
    "expired": {
        "en": "⌛ The approval for \"{what}\" expired, so I did not do it.",
        "hi-Latn": "⌛ \"{what}\" ka approval expire ho gaya, isliye nahi kiya.",
        "hi": "⌛ \"{what}\" की मंज़ूरी समाप्त हो गई, इसलिए नहीं किया।",
        "mr": "⌛ \"{what}\" ची मंजुरी संपली, म्हणून केले नाही.",
    },
}

_Q: dict[str, dict[str, str]] = {
    "kal_ambiguous": {"en": "By \"{clause}\" do you mean {o0} or {o1}?",
                      "hi-Latn": "\"kal\" se aapka matlab {o0} hai ya {o1}?",
                      "hi": "\"कल\" से आपका मतलब {o0} है या {o1}?", "mr": "तुम्हाला {o0} म्हणायचे आहे की {o1}?"},
    "next_weekday_ambiguous": {"en": "Which day exactly - {o0} or {o1}?",
                               "hi-Latn": "Kaunsa din - {o0} ya {o1}?", "hi": "कौन सा दिन - {o0} या {o1}?",
                               "mr": "नक्की कोणता दिवस - {o0} की {o1}?"},
    "am_pm_ambiguous": {"en": "Is that {o0} or {o1}?", "hi-Latn": "Subah ({o0}) ya shaam ({o1})?",
                        "hi": "सुबह ({o0}) या शाम ({o1})?", "mr": "सकाळी ({o0}) की संध्याकाळी ({o1})?"},
    "time_needed": {"en": "What time should I remind you?", "hi-Latn": "Kitne baje yaad dilaun?",
                    "hi": "कितने बजे याद दिलाऊँ?", "mr": "किती वाजता आठवण करून देऊ?"},
    "time_needed_for_period": {"en": "What time exactly?", "hi-Latn": "Exact kitne baje?",
                               "hi": "ठीक कितने बजे?", "mr": "नक्की किती वाजता?"},
    "draft_changed": {"en": "The draft changed after you approved it, so I did NOT send it. Ask me to prepare it "
                            "again and approve the new version.",
                      "hi-Latn": "Approve karne ke baad draft badal gaya, isliye maine NAHI bheja. Dobara draft "
                                 "karwa kar naya version approve kijiye.",
                      "hi": "मंज़ूरी के बाद ड्राफ्ट बदल गया, इसलिए मैंने नहीं भेजा। दोबारा ड्राफ्ट बनवाकर नया संस्करण मंज़ूर करें।",
                      "mr": "मंजुरीनंतर ड्राफ्ट बदलला, म्हणून मी पाठवला नाही. पुन्हा ड्राफ्ट करून नवीन आवृत्ती मंजूर करा."},
    "when_needed": {"en": "When should I remind you - which day and what time?",
                    "hi-Latn": "Kab yaad dilaun - kis din aur kitne baje?",
                    "hi": "कब याद दिलाऊँ - किस दिन और कितने बजे?",
                    "mr": "कधी आठवण करून देऊ - कोणत्या दिवशी आणि किती वाजता?"},
    "date_needed": {"en": "Which day?", "hi-Latn": "Kis din?", "hi": "किस दिन?", "mr": "कोणत्या दिवशी?"},
    "time_in_past": {"en": "That time has already passed. When should it be instead?",
                     "hi-Latn": "Yeh time nikal chuka hai. Kab rakhun?", "hi": "यह समय निकल चुका है। कब रखूँ?",
                     "mr": "ही वेळ निघून गेली आहे. कधी ठेवू?"},
    "past_relative_date": {"en": "That sounds like a past date - what should I do for it?",
                           "hi-Latn": "Yeh beeta hua din lag raha hai - kya karna hai?",
                           "hi": "यह बीता हुआ दिन लग रहा है - क्या करना है?", "mr": "हा मागचा दिवस वाटतो - काय करायचे?"},
    "train_origin_needed": {"en": "Which city are you travelling from?", "hi-Latn": "Kahan se travel karna hai?",
                            "hi": "कहाँ से यात्रा करनी है?", "mr": "कुठून प्रवास करायचा आहे?"},
    "train_destination_needed": {"en": "Where do you want to travel to?", "hi-Latn": "Kahan jaana hai?",
                                 "hi": "कहाँ जाना है?", "mr": "कुठे जायचे आहे?"},
    "train_date_needed": {"en": "Which date do you want to travel?", "hi-Latn": "Kis din jaana hai?",
                          "hi": "किस दिन जाना है?", "mr": "कोणत्या दिवशी जायचे आहे?"},
    "recipient_needed": {"en": "Who should this email go to? Please share their email address.",
                         "hi-Latn": "Yeh email kisko bhejna hai? Unka email address bata dijiye.",
                         "hi": "यह ईमेल किसे भेजना है? उनका ईमेल पता बताइए।",
                         "mr": "हा ईमेल कोणाला पाठवायचा? त्यांचा ईमेल पत्ता सांगा."},
    "store_choice": {"en": "🧪 TEST · Which app? Your list costs:", "hi-Latn": "🧪 TEST · Kaunsa app? Aapki list ka total:"},
    "weather_place_needed": {"en": "Which city or place should I check the weather for?",
                             "hi-Latn": "Kis sheher ka mausam dekhun?", "hi": "किस शहर का मौसम देखूँ?",
                             "mr": "कोणत्या शहराचे हवामान पाहू?"},
    "delivery_area_needed": {"en": "Where should I deliver? Send your area and city (e.g. Kothrud, Pune).",
                             "hi-Latn": "Delivery kahan karni hai? Area aur city bhejiye (jaise Kothrud, Pune).",
                             "hi": "डिलीवरी कहाँ करनी है? इलाका और शहर भेजिए (जैसे कोथरूड, पुणे)।",
                             "mr": "डिलिव्हरी कुठे करायची? भाग आणि शहर पाठवा (उदा. कोथरूड, पुणे)."},
    "tickets_needed": {"en": "How many tickets for {clause}?", "hi-Latn": "{clause} ke kitne tickets?"},
    "offer_details_needed": {"en": "To compare prices I need {clause}. Please tell me.",
                             "hi-Latn": "Price compare karne ke liye bataiye: {clause}?",
                             "hi": "कीमतें मिलाने के लिए बताइए: {clause}?", "mr": "किंमती तुलना करण्यासाठी सांगा: {clause}?"},
    "theatre_choice": {"en": "🧪 TEST · Which theatre? These are showing it:",
                       "hi-Latn": "🧪 TEST · Kaunsa theatre? Yahan chal rahi hai:"},
    "show_choice": {"en": "🧪 TEST · Which show?", "hi-Latn": "🧪 TEST · Kaunsa show?"},
    "show_full": {"en": "🧪 TEST · That show can't seat everyone together. Shows with seats:",
                  "hi-Latn": "🧪 TEST · Us show mein sab ek saath nahi baith sakte. In shows mein seats hain:"},
    "seats_unavailable": {"en": "🧪 TEST · Not enough seats together in the row you asked for. Free together:",
                          "hi-Latn": "🧪 TEST · Aapki row mein itni seats ek saath free nahi hain. Yeh free hain:"},
    "seats_taken": {"en": "🧪 TEST · Those seats were taken while waiting ({clause}). Nothing was charged. "
                          "Want me to look again?",
                    "hi-Latn": "🧪 TEST · Intezaar mein woh seats chali gayi ({clause}). Koi paisa nahi kata. "
                               "Phir se dhoondhun?"},
    "movie_city_needed": {"en": "Which city should I check {clause} in?",
                          "hi-Latn": "{clause} kis city mein dekhun?",
                          "hi": "{clause} किस शहर में देखूँ?",
                          "mr": "{clause} कोणत्या शहरात पाहू?"},
    "unknown_intent": {
        "en": "I can compare prices across apps and book the cheapest (TEST for now): products (Amazon, Flipkart, "
              "Croma...), food (Swiggy vs Zomato), groceries, flights, hotels, buses, events, restaurant tables and "
              "cab fares. I also search the web (news, IPOs, prices), set reminders with follow-ups, watch for price "
              "drops, keep a shopping list, check the weather, trains and movies, and handle email/calendar once "
              "you connect Google. What would you like?",
        "hi-Latn": "Main apps mein price compare karke sabse sasta book kar sakta hoon (abhi TEST): products (Amazon, "
                   "Flipkart...), khana (Swiggy vs Zomato), grocery, flights, hotels, bus, events, restaurant table, "
                   "cab fare. Web search (news, IPO, prices), reminders follow-up ke saath, price drop alert, "
                   "shopping list, mausam, trains, movies, aur Google connect karne par email/calendar. Kya karun?",
        "hi": "मैं सवालों के जवाब दे सकता हूँ, रिमाइंडर लगा सकता हूँ, ईमेल ड्राफ्ट कर सकता हूँ (आपकी मंज़ूरी के बाद ही "
              "जाएँगे), ट्रेन (IRCTC लिंक) और फ़िल्में (BookMyShow लिंक) ढूँढ सकता हूँ। क्या करूँ?",
        "mr": "मी प्रश्नांची उत्तरे देऊ शकतो, रिमाइंडर लावू शकतो, ईमेल ड्राफ्ट करू शकतो (तुमच्या मंजुरीनंतरच "
              "पाठवले जातील), ट्रेन (IRCTC लिंक) आणि चित्रपट (BookMyShow लिंक) शोधू शकतो. काय करू?"},
    "unsupported_request": {
        "en": "Sorry, I can't do this yet: {clause}. I can't log in to your accounts on other apps or pay bills "
              "there; real orders through those apps need partner access.",
        "hi-Latn": "Maaf kijiye, yeh abhi main nahi kar sakta: {clause}. Doosre apps mein aapke account se login "
                   "ya bill payment abhi nahi kar sakta; asli orders ke liye partner access chahiye.",
        "hi": "माफ़ कीजिए, यह अभी मैं नहीं कर सकता: {clause}। मैं आपके लिए ऑर्डर, पेमेंट या दूसरे ऐप/वेबसाइट पर "
              "काम अभी नहीं कर सकता।",
        "mr": "माफ करा, हे मी अजून करू शकत नाही: {clause}. मी तुमच्यासाठी ऑर्डर, पेमेंट किंवा इतर ॲप/वेबसाइटवर "
              "काम अजून करू शकत नाही."},
}


def lang_family(code: str | None) -> str:
    return {"hi": "hi", "mr": "mr", "hi-Latn": "hi-Latn", "mr-Latn": "hi-Latn"}.get(code or "en", "en")


def t(key: str, lang: str, **kw: Any) -> str:
    table = _T[key]
    return table.get(lang_family(lang), table["en"]).format(**kw)


# Question keys with localized text. Any other key renders (and is treated) as unknown_intent.
KNOWN_QUESTIONS = frozenset(_Q)


def question(key: str, lang: str, clause: str = "", options: list[str] | None = None) -> str:
    table = _Q.get(key, _Q["unknown_intent"])
    opts = (options or []) + ["", ""]
    return table.get(lang_family(lang), table["en"]).format(clause=clause, o0=opts[0], o1=opts[1])


from apna.agent.assist_replies import render_assist  # noqa: E402
from apna.agent.commerce_replies import render_commerce  # noqa: E402


def _when(iso: str) -> str:
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    # Stored times come back from the database in UTC; users read India time.
    return _dt.fromisoformat(iso).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%a %d %b, %I:%M %p").replace(
        ", 0", ", ")


def render_task_reply(lang: str, steps: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """steps: [{key, tool, description, status, result, error, approval_id?}]"""
    lines: list[str] = []
    actions: list[dict[str, Any]] = []
    for s in steps:
        status, tool, r = s["status"], s["tool"], s.get("result") or {}
        rendered = render_assist(s, lang, steps) or render_commerce(s, lang, steps)
        if rendered is not None:
            lines += rendered[0]
            actions += rendered[1]
            continue
        if status == "COMPLETED_LOCALLY" and tool == "ask_user":  # informational note, no answer needed
            icon = "🙏 " if r.get("question_key") == "unsupported_request" else "💬 "
            lines.append(icon + question(r.get("question_key", ""), lang, r.get("clause", ""), r.get("options")))
        elif status == "AWAITING_USER_ACTION" and tool == "ask_user":
            lines.append("❓ " + question(r.get("question_key", ""), lang, r.get("clause", ""), r.get("options")))
        elif status == "AWAITING_USER_ACTION" and r.get("choices") is not None and r.get("question_key"):
            # Questions with a numbered list of options (sandbox cinema: theatres, shows, rows)
            lines.append(question(r["question_key"], lang, r.get("clause", "")))
            lines += [f"{i}. {c}" for i, c in enumerate(r["choices"], 1)] or ["(none found)"]
            if r["choices"]:
                lines.append({"hi-Latn": "Number ya naam likh kar reply kijiye."}.get(
                    lang_family(lang), "Reply with a number or name."))
        elif status == "AWAITING_USER_ACTION" and r.get("question_key"):
            lines.append("❓ " + question(r["question_key"], lang))
        elif status == "AWAITING_USER_ACTION" and r.get("handoff"):
            lines.append(t("handoff", lang, site=r.get("site", "")))
        elif status == "SCHEDULED" and tool == "create_reminder":
            lines.append(t("reminder_set", lang, when=r["local_display"], text=r["text"]))
        elif status == "COMPLETED_LOCALLY" and tool == "draft_email":
            lines.append(t("draft_ready", lang, to=r["to_address"], subject=r["subject"], body=r["body"]))
        elif status == "COMPLETED_LOCALLY" and tool == "find_email":
            continue
        elif status == "COMPLETED_LOCALLY" and tool == "book_movie_tickets" and r.get("kind") == "held":
            from apna.providers.cinema import rupees

            when = _when(r["starts_at"])
            asked = [x for x in (r.get("requested_rows") or []) if x]
            got_row = r["seats"][0][:1] if r.get("seats") else ""
            note = f" (row {'/'.join(asked)} as asked)" if asked and got_row in asked else ""
            lines.append(t("cinema_held", lang, movie=r["movie"], theatre=r["theatre"], when=when,
                           seats=", ".join(r["seats"]), cls=r["seat_class"], price=rupees(r["unit_price_minor"]),
                           note=note, total=rupees(r["amount_minor"]), mins=r.get("hold_minutes", 15)))
        elif status == "COMPLETED_LOCALLY" and tool == "book_movie_tickets":
            lines.append(t("cinema_not_listed", lang, movie=r.get("movie", ""), link=r.get("bms_link", "")))
        elif status == "AWAITING_APPROVAL" and tool == "pay_movie_booking":
            from apna.providers.cinema import rupees

            lines.append(t("cinema_pay", lang, total=rupees(int(r.get("amount_minor") or 0))))
            actions.append({"type": "approval", "approval_id": s.get("approval_id"), "step_key": s["key"]})
        elif status == "CONFIRMED_BY_PROVIDER" and tool == "pay_movie_booking":
            from apna.providers.cinema import rupees

            link = next((x.get("result", {}).get("bms_link") for x in steps
                         if x["tool"] == "book_movie_tickets" and (x.get("result") or {}).get("bms_link")), "")
            lines.append(t("cinema_booked", lang, code=r["booking_code"], movie=r["movie"], theatre=r["theatre"],
                           when=_when(r["starts_at"]), seats=", ".join(r["seats"]), cls=r["seat_class"],
                           total=rupees(r["amount_minor"]), ref=r["payment_ref"], link=link))
        elif status == "COMPLETED_LOCALLY" and tool == "answer_question":
            lines.append(r.get("answer", ""))
        elif status == "COMPLETED_LOCALLY" and tool == "find_movie_tickets":
            city = (r.get("city") or "").title()
            if r.get("exact"):
                from datetime import date as _d

                day = _d.fromisoformat(r["show_date"]).strftime("%a %d %b") if r.get("show_date") else ""
                extra = f" · {r['tickets']} tickets" if r.get("tickets") else ""
                lines.append(t("movie_show_page", lang, movie=r.get("movie", ""), city=city, day=day, extra=extra,
                               link=r["official_link"]))
            else:
                lines.append(t("movie_unknown", lang, movie=r.get("movie", ""), city=f" - {city}" if city else "",
                               link=r["official_link"]))
        elif status == "COMPLETED_LOCALLY" and tool == "search_trains":
            lines.append(t("trains_link", lang, o=r["origin"].title(), d=r["destination"].title(), date=r["travel_date"],
                           cls=f", {r['travel_class']}" if r.get("travel_class") else "",
                           pref=f", {r['time_preference']}" if r.get("time_preference") else "",
                           os=", ".join(x["code"] for x in r["origin_stations"]),
                           ds=", ".join(x["code"] for x in r["destination_stations"]), link=r["official_link"]))
        elif status == "COMPLETED_LOCALLY" and tool == "browser_read_page":
            lines.append(t("page_read", lang, site=r.get("site", ""), summary=r.get("headline", "")))
            if r.get("untrusted_instructions_detected"):
                lines.append(t("injection_note", lang))
        elif status == "AWAITING_APPROVAL" and tool == "send_email":
            lines.append(t("approve_send", lang, to=r.get("to_address", "?")))
            actions.append({"type": "approval", "approval_id": s.get("approval_id"), "step_key": s["key"]})
        elif status == "AWAITING_APPROVAL" and tool == "browser_submit_form":
            lines.append(t("approve_form", lang, site=r.get("site", ""), fields=r.get("fields_preview", "")))
            actions.append({"type": "approval", "approval_id": s.get("approval_id"), "step_key": s["key"]})
        elif status in {"CONFIRMED_BY_PROVIDER", "SUBMITTED_EXTERNALLY"} and tool == "send_email":
            key = "sent_sandbox" if r.get("environment") == "mock" else "sent_real"
            lines.append(t(key, lang, to=r["to_address"], id=r["provider_message_id"], provider=r.get("provider", "")))
        elif status == "CONFIRMED_BY_PROVIDER" and tool == "browser_submit_form":
            lines.append(t("form_confirmed", lang, site=r.get("site", ""), code=r.get("confirmation_code", "")))
        elif status == "REJECTED":
            if tool == "pay_grocery_order":
                lines.append({"hi-Latn": "✋ Theek hai, order cancel. Koi paisa nahi kata."}.get(
                    lang_family(lang), "✋ Okay, order cancelled. Nothing was charged."))
            elif tool == "pay_movie_booking":
                lines.append({"hi-Latn": "✋ Theek hai, booking cancel. Koi paisa nahi kata; seats chhod di."}.get(
                    lang_family(lang), "✋ Okay, booking cancelled. Nothing was charged and the seats are released."))
            else:
                lines.append(t("not_sent", lang) if tool == "send_email" else f"✋ {s['description']}: cancelled.")
        elif status == "SKIPPED" and tool in ("pay_movie_booking", "pay_grocery_order"):
            continue  # the seat question above explains; payment comes after the user's answer
        elif status == "SKIPPED":
            lines.append(t("skipped", lang, what=s["description"]))
        elif status == "CANCELLED" and r.get("expired"):
            lines.append(t("expired", lang, what=s["description"]))
        elif status == "FAILED":
            lines.append(t("failed", lang, what=s["description"], why=s.get("error") or "unknown error"))
    return "\n\n".join(lines), actions
