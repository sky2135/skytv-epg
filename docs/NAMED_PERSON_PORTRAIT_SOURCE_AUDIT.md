# Named-person portrait source audit

Reviewed 2026-09-20. This is a source-research catalog, not a production image catalog. A portrait appears in the TV guide only after its source is approved, the cutout is rendered and visually checked, and the finished asset is added to `assets/logos/icon_catalog.csv` with attribution.

## Result

| Decision | People | Production action |
| --- | ---: | --- |
| Approved source | 168 | Eligible for cutout rendering and visual QA |
| Conditional source | 22 | Keep the neutral role fallback until the stated issue is resolved |
| No verified free portrait | 31 | Keep the neutral role fallback |
| **Total** | **221** | **No fuzzy person matching** |

The current inventories contain 231 person-channel rows covering 220 exact people. All 231 rows have an exact research record. Sidhu Moose Wala is the 221st subject and is prepared for a future exact channel only. Of the 141 sources approved in the initial audit, 133 new current-channel candidates entered the Version 1 rendering pass. The later 27 source approvals described below do not change production status by themselves; six of them separately passed output QA and entered production.

The scan found 33 people across 42 current channel rows that the earlier named-person parser missed. Those rows are now covered by reviewed exact category/channel rules, including surname and acronym aliases such as Hitchcock and JCVD; broad 24/7 categories still never use fuzzy person matching. Four provider spellings also have exact corrections: `FAKHIR MEHMOOD` → Faakhir Mehmood, `GOHAR MUMTAZ` → Goher Mumtaz, `HUMAIRA CHANNA` → Humera Channa, and `RAJKUMAAR RAO` → Rajkummar Rao.

## Decision rules

- `approved` requires an exact-person source page and a verified public-domain, CC0, CC BY, or compatible CC BY-SA license.
- `conditional` is not approved. It remains on the neutral microphone or movie symbol until its identity, provenance, composition, watermark, or resolution condition is resolved.
- `no_verified_free_portrait` means web results were found but no exact, suitably licensed real portrait passed review.
- A publicly visible Google result is not automatically reusable. The original file page and its license control the decision. See [Wikimedia Commons reuse guidance](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia/en).
- Attribution, license links, and modification notices must follow the applicable Creative Commons terms. ShareAlike derivatives stay under a compatible license. See the [CC BY-SA 4.0 deed](https://creativecommons.org/licenses/by-sa/4.0/).
- Copyright permission does not remove privacy, personality, or publicity-right considerations. Use portraits only to identify the channel subject, never to imply endorsement. See [Commons personality-rights guidance](https://commons.wikimedia.org/wiki/Commons:Personality_rights).

## Approved production treatment

- Preserve the exact photographed identity, expression, hair or turban, clothing, skin tone, and pose; do not invent, beautify, or substitute a likeness.
- Isolate a centered head-and-upper-torso portrait on a genuinely transparent square canvas.
- Use the same restrained neutral contour as the guide symbols: a thin off-white inner edge (`#F7F8FA`) and a slightly thicker dark outer edge (`#1B2230`).
- Add no text, logo, card, gradient, scenery, or coloured background.
- Check every result against its source on both dark and light guide backgrounds before adding it to the production catalog.

## Sidhu Moose Wala

The selected candidate is [Sidhu Moose Wala during the shooting of *Moosa Jatt* (cropped)](https://commons.wikimedia.org/wiki/File:Sidhu_Moose_Wala_during_the_shooting_of_his_film_Moosa_Jatt_%28cropped%29.jpg), 2419×2849, by Shekha Gill, licensed CC-BY-SA-4.0. It is approved as a prepared-future source, but it must not bind to Balkar Sidhu or any other name. The reviewed channel snapshots do not contain an exact Sidhu Moose Wala channel.

## Conditional-source follow-up

All 49 conditional rows were re-reviewed on 2026-09-20. Twenty-seven now have defensible exact identity, provenance, reuse terms, and a subject-specific crop path, so they moved to `approved`; 22 remain `conditional` and must keep the neutral role fallback.

Newly source-approved: Akhil, Amar Noorie, Anuradha Paudwal, Babbu Maan, Baljit Malwa, Bohemia, Boman Irani, Daler Mehndi, Farhan Saeed, Gul Panra, Guri, Guru Randhawa, Happy Raikoti, Kunal Ganjawala, Labh Janjua, Malkit Singh, Mickey Singh, Naseeruddin Shah, Raj Babbar, Randeep Hooda, Ravinder Grewal, Rishi Kapoor, Shammi Kapoor, Sukhwinder Singh, Tarsem Jassar, Vinod Khanna, and Yuvraj Hans.

This is source approval only. It does not assert that a generated cutout is faithful, sharp enough, correctly isolated, or production-ready. Each candidate remains on its fallback until a separate full-resolution output review passes identity, source fidelity, transparency, contour, and guide-size checks and the result is added to the catalog and attribution table. Low-resolution sources may therefore be approved for lawful use while every attempted portrait output is still rejected.

### Conditional-pass production outcome

Every one of the 27 cleared sources received an explicit output disposition. Six transparent cutouts passed the separate source-fidelity and visual review and were added to production: Akhil, Baljit Malwa, Boman Irani, Farhan Saeed, Guri, and Naseeruddin Shah. The other 21 source-cleared subjects retain the neutral fallback because the available output reconstructed low-resolution or occluded details. Together with the 22 still-conditional sources, 43 members of the original conditional set remain on fallback.

| Decision | People | Production action |
| --- | ---: | --- |
| Accepted conditional-pass portrait | 6 | Add the reviewed 512×512 transparent cutout, catalog row, and attribution row |
| Source approved; output rejected | 21 | Keep the neutral fallback until a source-faithful cutout exists |
| Source still conditional | 22 | Keep the neutral fallback until the source issue is resolved |
| **Original conditional set** | **49** | **Only the six accepted outputs enter the TV guide** |

The production catalog now contains 113 exact-name person portraits: seven pre-existing portraits, 100 accepted in the Version 1 pass, and six accepted in this follow-up.

## Version 1 production outcome

All 133 approved new current-channel candidates were processed on 2026-09-19. A second reviewer compared every available result with its licensed source at full resolution. Only 100 new portraits passed source, identity, transparency, contour, and source-fidelity checks; the other 33 deliberately remain on the neutral role fallback. The seven approved portraits already in the repository remain unchanged, and Sidhu Moose Wala remains prepared for a future exact channel only.

| Decision | People | Production action |
| --- | ---: | --- |
| Accepted new portrait | 100 | Add the reviewed 512×512 transparent cutout to `people/`, the asset catalog, and attribution table |
| Initial production reject | 6 | Keep the neutral fallback; no acceptable output survived the first review |
| Independent visual-QA hold | 27 | Keep the neutral fallback; do not publish the generated output |
| **Total processed** | **133** | **No failed or held output enters the TV guide** |

Initial rejects: Amrish Puri (no output after moderation), Dr. Rajkumar (telephone and hand obstruct the face), Gurshabad and Jackie Shroff (no attributable output after a safety rejection), Louis Theroux (transparent/contour artifact inside an eyeglass lens), and Michael McIntyre (invented sharp facial detail from a motion-blurred source).

The independent review also held these otherwise licensed sources:

| Person | Hold reason |
| --- | --- |
| Ajay Devgn | The small source did not support the reconstructed face, beard, eye, and hair detail |
| Ali Azmat | The 288×301 source did not support the generated facial detail |
| Ali Zafar | Unsupported face/hair detail, changed hair volume, and removed shirt artwork |
| Baba Sehgal | Regenerated face, hand, jewellery, and garment detail |
| David Attenborough | The tiny source face did not support the generated wrinkles, hair, eyes, and teeth |
| Emraan Hashmi | Unsupported face, hair, beard, and clothing reconstruction |
| H Dhami | Microphone/arm removal changed the hand interaction and synthesized hidden clothing |
| Jean-Claude Van Damme | Invented texture and altered glasses tint/reflections |
| Kader Khan | Unsupported face detail and cane-removal synthesis over hidden clothing |
| Kavita Krishnamurti | Material relighting and recolouring changed the source skin and sari balance |
| Lucky Ali | Microphone removal reconstructed the gripping hands into a different pose |
| Micky Flanagan | The low-resolution source did not support the generated hair, skin, and teeth detail |
| Mohit Chauhan | A retained microphone blocks the mouth and lower face |
| Mukesh | The tiny historical source face did not support the generated facial and suit detail |
| Mustafa Zahid | Unsupported face, hair, clothing, and jersey-lettering reconstruction |
| Neha Kakkar | The tiny full-body source did not support the generated makeup, teeth, skin, and hair detail |
| Nusrat Fateh Ali Khan | Microphone removal invented regions of the face, ear, hair, and shirt hidden in the source |
| Palak Muchhal | An invented opaque white glyph is attached to the hair edge |
| Prajwal Devaraj | Hair/contour is clipped at the canvas edge and facial detail exceeds the source |
| Rahul Dev | Unsupported face/hair detail and a changed hair silhouette |
| Ranveer Singh | The compact source did not support the sharpened face, hair, beard, and garment detail |
| Saif Ali Khan | The 286×400 source was heavily synthesized in the face, hair, moustache, and clothing |
| Sanjay Dutt | Object removal reconstructed an unverified torso and forearm pose |
| Shiva Rajkumar | The 264×334 source did not support the generated face, hair, jewellery, and shirt detail |
| Shreya Ghoshal | Regenerated facial/hair/shirt detail plus a coloured edge remnant |
| Tom Hardy | T-shirt artwork and lettering were regenerated into different, garbled detail |
| Tulsi Kumar | The tiny full-body source did not support the generated makeup, hair, jewellery, and beading |

## Conditional sources

| Person | Candidate | Reason or release condition |
| --- | --- | --- |
| Aima Baig | [source](https://commons.wikimedia.org/wiki/File:Aima_Baig_VOA_2023.png) | HOLD - DO NOT USE YET VOA republishes third-party material, so the agency name alone does not establish that this frame is a U.S.-government work; require a completed license review or direct VOA ownership proof Upstream source: https://facebook.com/VOAUrdu/videos/838140957668261. Source license statement: Public domain claimed as U.S. federal-government work. |
| Ambareesh | [source](https://commons.wikimedia.org/wiki/File:Ambarish.jpg) | Do not deploy yet: Ambareesh died in 2018, while this file claims own work dated 2022. A separate verified government photograph exists under GODL-India, but that license is outside this task's strict PD/CC allowlist. Source license statement: CC0 1.0 (tagged, provenance unresolved). |
| Arif Lohar | [source](https://commons.wikimedia.org/wiki/File:Arif_Lohar_2021.png) | HOLD - DO NOT USE YET Require a completed YouTube license review or archived source showing the compatible license on the cited video Upstream source: https://youtube.com/watch?v=T7vdYmyC7Uk&t=46s. Source license statement: CC BY 3.0 claimed from YouTube. |
| Bruce Lee | [source](https://commons.wikimedia.org/wiki/File:Bruce_Lee_1973.jpg) | Conditional on deployment jurisdiction: the file page says it is public domain in the United States because it was published in 1931-1977 without a copyright notice, and explicitly warns it may remain copyrighted in countries that do not apply the rule of the shorter term. Source license statement: Public domain in the United States (PD-US-no notice). |
| Deep Jandu | [source](https://commons.wikimedia.org/wiki/File:Deep_Jandu.jpg) | HOLD - DO NOT USE YET. Identity is consistent with Deep Jandu, but the polished campaign-style image has only an unsupported uploader own-work claim, no photographer or original-source chain, and no VRT evidence. Require rightsholder confirmation; do not treat article use as copyright proof. Source license statement: CC BY-SA 4.0 claimed. |
| Ghulam Abbas | [source](https://en.wikipedia.org/wiki/File:Ghulamabas.jpg) | Research identity label normalized from "Ghulam Abbas - Pakistani playback and classical singer, born 1955" to "Ghulam Abbas"; keep the identity evidence when reviewing namesakes. HOLD - DO NOT USE YET Identity matches the 1955 singer and his article, not another Ghulam Abbas; require author/source evidence or a human-validated Commons transfer before use Source license statement: CC0 claimed. |
| Humera Channa | [source](https://commons.wikimedia.org/wiki/File:Humera_Channa_2019.png) | Reviewed provider spelling or alias: "HUMAIRA CHANNA" maps exactly to "Humera Channa". HOLD - DO NOT USE YET Require a completed YouTube license review or archived proof of the cited video's compatible license Upstream source: https://youtube.com/watch?v=aeLF1EhCov4&t=28s. Source license statement: CC BY 3.0 claimed from YouTube. |
| Imran Khan | [source](https://commons.wikimedia.org/wiki/File:Imran_Khan_Singer.jpg) | Research identity label normalized from "Imran Khan - Dutch-Pakistani singer and rapper" to "Imran Khan"; keep the identity evidence when reviewing namesakes. HOLD - DO NOT USE YET Confirm the channel means the singer/rapper, not the politician, and obtain completed source-license verification before use Upstream source: https://m.youtube.com/watch?v=4qnPkSnpicI. Source license statement: CC BY 3.0 claimed from YouTube. |
| Inayat Hussain Bhatti | [source](https://commons.wikimedia.org/wiki/File:Late_.aniyat_husain_bhatti.jpg) | HOLD - DO NOT USE YET A scan or copy is not automatically the uploader's copyright; require evidence that the uploader photographed the singer or controls the original photograph's rights Source license statement: CC BY-SA 4.0 claimed. |
| Kishore Kumar | [source](https://commons.wikimedia.org/wiki/File:Kishore_Kumar_2016_postcard_of_India_(cropped).jpg) | Reviewed provider spelling or alias: "KISHOR KUMAR" maps exactly to "Kishore Kumar". Match only as reviewed alias: KISHOR KUMAR → Kishore Kumar GODL-India is a free government-data license rather than CC/CC0. Confirm that the project accepts this license and reproduce the required India Post attribution; otherwise use the microphone fallback. Follow GODL-India attribution and non-endorsement terms; document the crop as a modification. Copyright license does not waive personality/publicity rights; use as factual EPG identification, not endorsement, and review local requirements before commercial release. neutral microphone icon |
| Lokesh | [source](https://commons.wikimedia.org/wiki/File:Lokesh.jpg) | Do not deploy yet: Lokesh died in 2004, but the uploader labels this 2005 image as own work; FBMD metadata suggests a social-media derivative. Obtain provenance/date clarification before relying on the license claim. Source license statement: CC BY-SA 4.0 (tagged, provenance unresolved). |
| Mohammed Rafi | [source](https://commons.wikimedia.org/wiki/File:Mohammed_Rafi_2016_postcard_of_India_crop-flip.jpg) | Reviewed provider spelling or alias: "MOHAMMAD RAFI" maps exactly to "Mohammed Rafi". Match only as reviewed alias: MOHAMMAD RAFI → Mohammed Rafi GODL-India is a free government-data license rather than CC/CC0. Confirm that the project accepts this license and reproduce the required India Post attribution; otherwise use the microphone fallback. Follow GODL-India attribution and non-endorsement terms; document the crop as a modification. Copyright license does not waive personality/publicity rights; use as factual EPG identification, not endorsement, and review local requirements before commercial release. neutral microphone icon |
| Naseebo Lal | [source](https://commons.wikimedia.org/wiki/File:Naseebo_Lal_2017_(2).png) | HOLD - DO NOT USE YET Require completed source-license review and test the face crop at final icon size Upstream source: https://youtube.com/watch?v=41jfXE5DMoM&t=37s. Source license statement: CC BY 3.0 claimed from YouTube. |
| Noor Jehan | [source](https://commons.wikimedia.org/wiki/File:Noor_Jehan_%26_Lata_Mangeshkar.jpg) | Research identity label normalized from "Noor Jehan - Pakistani singer and actress, 1926-2000" to "Noor Jehan"; keep the identity evidence when reviewing namesakes. HOLD - DO NOT USE YET Do not use without photographer/rightsholder evidence. Humjoli 1946.jpg and Noor Jehan in Zeenat.jpg were also rejected because Pakistan/India public-domain claims lacked a secure U.S. public-domain basis, with Humjoli under deletion review during this audit Source license statement: CC BY-SA 4.0 claimed. |
| Preet Harpal | [source](https://commons.wikimedia.org/wiki/File:Preet_Harpal.jpg) | YouTube-origin still licensed on the Commons page as CC BY 4.0. Keep conditional because of modest resolution and reliance on the source channel's license assertion. Source license statement: CC BY 4.0. |
| Prem Dhillon | [source](https://commons.wikimedia.org/wiki/File:Prem_Dhillon_Majha_Block.jpg) | YouTube-origin screenshot. Commons lists CC BY 3.0 but the file page does not show the explicit review marker used by stronger reviewed YouTube sources; retain conditionally. Source license statement: CC BY 3.0. |
| Rajendra Kumar | [source](https://commons.wikimedia.org/wiki/File:Rajendra_Kumar.jpg) | Reviewed provider spelling or alias: "RAJENDR KUMAR" maps exactly to "Rajendra Kumar". Inventory spelling normalized to Rajendra Kumar; Commons records email-confirmed permission; resolution is the limiting factor. BollywoodDirect Source license statement: CC BY-SA 4.0. |
| Ranjit Bawa | [source](https://commons.wikimedia.org/wiki/File:Ranjit_Bawa_lnterview.jpg) | YouTube-origin screenshot. Commons lists CC BY 3.0, but the raw file page does not expose the explicit review marker seen on the older reviewed ABP Sanjha files; retain conditionally. Source license statement: CC BY 3.0. |
| Sarbjit Cheema | [source](https://commons.wikimedia.org/wiki/File:Sarabjit_Cheema_%28cropped%29.jpg) | Legally free on the Commons page, but not recommended for production. Do not remove the source watermark without a separately reviewed workflow; retain share-alike if used. Source license statement: CC BY-SA 4.0. |
| Sukshinder Shinda | [source](https://commons.wikimedia.org/wiki/File:Photoshute_pic_2014-05-08_11-52.jpg) | HOLD - DO NOT USE YET. The polished studio portrait comes from a one-file-style uploader and supplies no event, photographer, original source, capture date, useful camera EXIF, or VRT record. Require rightsholder confirmation; no stronger exact reusable portrait was found. Source license statement: CC BY-SA 3.0 claimed. |
| Surjit Bindrakhia | [source](https://commons.wikimedia.org/wiki/File:Surjit_Bindrakhia.jpg) | HOLD - DO NOT USE YET. The file claims own work dated 2020 although Surjit Bindrakhia died in 2003, and it gives no original photographer, scan/digitization chain, inherited-rights evidence, or camera-date EXIF. A possible family connection cannot be inferred from the username. Source license statement: CC BY-SA 4.0 claimed. |
| Surjit Khan | [source](https://commons.wikimedia.org/wiki/File:Surjit_Khan_Singer.jpg) | HOLD - DO NOT USE YET. The one-file uploader provides only an unsupported own-work claim; Adobe Photoshop and FBMD/Facebook metadata indicate a social-media derivative rather than an original camera file. Require the photographer/rightsholder source chain. Source license statement: CC BY-SA 4.0 claimed. |

## No verified free portrait

| Person | Research result |
| --- | --- |
| Akhlaq Ahmed | Research identity label normalized from "Akhlaq Ahmed - Pakistani playback singer, 1946-1999" to "Akhlaq Ahmed"; keep the identity evidence when reviewing namesakes. NO ASSET Do not substitute similarly named people or an unlicensed press/tribute image |
| Aman Hayer | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Amar | Strictly excluded as ambiguous. Results for Amar Noorie belong to a separate canonical subject and were not reused for this row. |
| Angrej Ali | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Babbal Rai | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Balkar Sidhu | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Bharat Bhushan | Research identity label normalized from "Bharat Bhushan (Hindi-film actor, 1920-1992)" to "Bharat Bhushan"; keep the identity evidence when reviewing namesakes. Rejected the file Gbharatbhushan.jpg and its category because they depict a different person. Also rejected unlicensed publicity and film-still results. Source license statement: N/A. |
| Bikram Singh | Commons search results were the Indian army chief and politician Bikram Singh/Majithia, not the Punjabi-American singer. Those false matches were rejected. |
| Dev Dhillon | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Dharmpreet | Exact-name Commons bitmap search found only unrelated Sikh-history graphics; no exact singer portrait was accepted. |
| Durga Rangila | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Faakhir Mehmood | Reviewed provider spelling or alias: "FAKHIR MEHMOOD" maps exactly to "Faakhir Mehmood". NO ASSET Do not treat an autograph, logo, album cover, or unlicensed publicity image as a portrait |
| Falak Shabir | NO ASSET Search results were publicity, social-media, or press imagery without a qualifying reuse license |
| Harjit Harman | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Johnny Walker | Research identity label normalized from "Johnny Walker (Badruddin Jamaluddin Kazi, Indian actor)" to "Johnny Walker"; keep the identity evidence when reviewing namesakes. Reviewed provider spelling or alias: "JHONY WALKER" maps exactly to "Johnny Walker". Rejected all silent-film-actor public-domain files as wrong identity, plus unlicensed editorial/film-still results. Source license statement: N/A. |
| Lakhwinder Singh | Exact-name Commons and English Wikipedia checks did not resolve a unique Punjabi singer or an exact reusable portrait. |
| Lehmber Hussainpuri | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Maninder Buttar | The English Wikipedia article has no Commons portrait, and exact-name Commons bitmap search found no acceptable file. |
| Manmohan Waris | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Mehmood | Research identity label normalized from "Mehmood Ali" to "Mehmood"; keep the identity evidence when reviewing namesakes. Rejected: GODL-India is outside the stated PD/CC allowlist, and the asset is a stamp rather than a real photograph. Getty/press/fan results lacked reusable-license evidence. Keep the neutral actor fallback. [1] [2] |
| Nabeel Shaukat Ali | Reviewed provider spelling or alias: "NABEEL SHOUKAT" maps exactly to "Nabeel Shaukat Ali". NO ASSET Do not reuse Spotify, SoundCloud, album-art, or press images without an explicit qualifying license |
| Nachhatar Gill | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Navv Inder | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Nayyara Noor | NO ASSET Available results were press, obituary, album, or social-media images without a reusable license |
| Pammi Bai | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Pav Dharia | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Rahim Shah | Research identity label normalized from "Raheem Shah / Rahim Shah - Pakistani pop singer, born 1975" to "Rahim Shah"; keep the identity evidence when reviewing namesakes. NO ASSET Do not substitute unrelated people or unlicensed Pak101, Pakpedia, press, or social-media images |
| Sardool Sikander | The English Wikipedia article uses a local wikipedia/en image, not a Commons original; it is treated as non-free/fair-use and rejected. Commons exact-name search produced no exact alternative. |
| Satwinder Bitti | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |
| Shankar Nag | Rejected evidence: https://commons.wikimedia.org/wiki/File:Bronze_Statue_of_Shankara_Nag,_80_feet_Road,_TippaSandra_Entrance_Bus_Stop,_Indiranagar,_Bengaluru.jpg and https://commons.wikimedia.org/wiki/File:Shankar.jpg . Use the neutral movie fallback, never the name-collision image. |
| Sheera Jasvir | Exact-name Commons bitmap search and English Wikipedia/Wikidata image check on 2026-09-19 found no acceptable file. |

## All researched people

The CSV beside this report contains the full provenance, dimensions, identity evidence, crop notes, and fallback asset for every row.

| Person | Role family | Status | License | File page |
| --- | --- | --- | --- | --- |
| A. R. Rahman | singer | Approved | CC-BY-4.0 | [source](https://commons.wikimedia.org/wiki/File:A.R._Rahman_at_the_2025_Toronto_International_Film_Festival.jpg) |
| Abhay Deol | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Abhay_Deol_in_2021_(2).jpg) |
| Abhijeet Bhattacharya | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Abhijeet_Bhattacharya.jpg) |
| Abhishek Bachchan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Abhishek_Bachchan_in_2025.jpg) |
| Abrar-ul-Haq | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Abrar-ul-Haq_2014-05-10.jpg) |
| Adnan Sami | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Adnansami2.jpg) |
| Aftab Shivdasani | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Aftab_Shivdasani_(IIFA-2014).jpg) |
| Aima Baig | singer | Conditional | PD-USGov | [source](https://commons.wikimedia.org/wiki/File:Aima_Baig_VOA_2023.png) |
| Ajay Devgn | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Ajay_Devgn_promotes_Baadshaho.jpg) |
| Akhil | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Akhil.jpg) |
| Akhlaq Ahmed | singer | No verified free portrait | — | — |
| Akshay Kumar | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Akshay_Kumar_in_2022.jpg) |
| Al Pacino | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Al_Pacino_in_2016.jpg) |
| Alfred Hitchcock | actor | Approved | PD-US | [source](https://commons.wikimedia.org/wiki/File:Alfred_Hitchcock_NYWTS.jpg) |
| Ali Azmat | singer | Approved | PD-Self | [source](https://commons.wikimedia.org/wiki/File:Ali_in_Orange.jpg) |
| Ali Zafar | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Ali_zafar.jpg) |
| Alisha Chinai | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Alisha_Chinai_2009_-_still_64293_crop.jpg) |
| Alka Yagnik | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Alka_Yagnik.jpg) |
| Aman Hayer | singer | No verified free portrait | — | — |
| Amar | singer | No verified free portrait | — | — |
| Amar Noorie | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Amar_Noorie_with_her_son_Tejasbir_Singh.jpg) |
| Ambareesh | actor | Conditional | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Ambarish.jpg) |
| Amitabh Bachchan | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Indian_actor_Amitabh_Bachchan.jpg) |
| Amrish Puri | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Amrish_Puri.jpg) |
| Amrit Maan | singer | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Amrit_Maan-_41365565990_%28cropped%29.jpg) |
| Angrej Ali | singer | No verified free portrait | — | — |
| Anil Kapoor | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Anil_Kapoor_snapped_at_Race_3_interviews_at_Sun_N_Sand_hotel_in_Juhu.jpg) |
| Anupam Kher | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Anupam_Kher_(40740364673)_(cropped).jpg) |
| Anuradha Paudwal | singer | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:%22Yeh_Ishq_Aur_Yeh_Barish%22_Song_Recording_Time_Sarbarish_Majumder_%26_Anuradha_Paudwal.jpg) |
| Arif Lohar | singer | Conditional | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Arif_Lohar_2021.png) |
| Arijit Singh | singer | Approved | CC-BY-4.0 | [source](https://commons.wikimedia.org/wiki/File:Arijit_Singh_performance_at_Chandigarh_2025.jpg) |
| Arnold Schwarzenegger | actor | Approved | PD-USGov | [source](https://commons.wikimedia.org/wiki/File:Arnold_Schwarzenegger.JPG) |
| Asha Bhosle | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Asha_Bhosle_at_Bhubaneswar.jpg) |
| Atif Aslam | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Atif_Aslam_in_black_coat.jpg) |
| Ayushmann Khurrana | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Ayushmann_Khurrana_at_Grazia_Millennial_Awards,_2022.jpg) |
| B Praak | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:B_Praak.jpg) |
| Baba Sehgal | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Baba_Sehgal_shoots_for_his_album_%27Mumbai_City%27_02.jpg) |
| Babbal Rai | singer | No verified free portrait | — | — |
| Babbu Maan | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Babbu_Maan_Baarish_Ke_Bahaane.jpg) |
| Baljit Malwa | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Baljit_Malwa.jpg) |
| Balkar Sidhu | singer | No verified free portrait | — | — |
| Bharat Bhushan | actor | No verified free portrait | — | — |
| Bikram Singh | singer | No verified free portrait | — | — |
| Bilal Saeed | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Bilal_Saeed.jpg) |
| Billy Connolly | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Billy_Connolly_(26221271743)_(cropped).jpg) |
| Bobby Deol | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Bobby_Deol_graces_the_special_screening_of_%E2%80%98Poster_Boys%E2%80%99.jpg) |
| Bohemia | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Bohemia_performing.jpg) |
| Boman Irani | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:IIFA_2017_Green_Carpet_(36349841166)_(cropped).jpg) |
| Bruce Lee | actor | Conditional | PD-US-No-Notice | [source](https://commons.wikimedia.org/wiki/File:Bruce_Lee_1973.jpg) |
| Chris Rock | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Chris_Rock_2014.jpg) |
| Clint Eastwood | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Clint_Eastwood_at_2010_New_York_Film_Festival.jpg) |
| Conor McGregor | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Conor_McGregor,_UFC_189_World_Tour_London.jpg) |
| Daler Mehndi | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Daler_Mehndi_at_the_Audio_release_of_%27Kyaa_Super_Kool_Hain_Hum%27_08.jpg) |
| David Attenborough | actor | Approved | CC-BY-2.5 | [source](https://commons.wikimedia.org/wiki/File:David_Attenborough.jpg) |
| Deep Jandu | singer | Conditional | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Deep_Jandu.jpg) |
| Dev Anand | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Dev_Anand_still1.jpg) |
| Dev Dhillon | singer | No verified free portrait | — | — |
| Dharmendra | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Dharmendra.jpg) |
| Dharmpreet | singer | No verified free portrait | — | — |
| Dilip Kumar | actor | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Dilip_Kumar.jpg) |
| Dr. Rajkumar | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Dr._Rajkumar_(3).jpg) |
| Durga Rangila | singer | No verified free portrait | — | — |
| Eddie Murphy | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Eddie_Murphy_2010.jpg) |
| Emraan Hashmi | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Emraan_Hashmi_spotted_in_Bandra.jpg) |
| Faakhir Mehmood | singer | No verified free portrait | — | — |
| Falak Shabir | singer | No verified free portrait | — | — |
| Farhan Akhtar | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Farhan_Akhtar_-_Times_Litfest_2016,_New_Delhi.jpg) |
| Farhan Saeed | singer | Approved | CC-BY-2.5 | [source](https://commons.wikimedia.org/wiki/File:Jal_MAA.jpg) |
| Feroz Khan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Feroz_Khan.jpg) |
| Ghulam Abbas | singer | Conditional | CC0-1.0 | [source](https://en.wikipedia.org/wiki/File:Ghulamabas.jpg) |
| Goher Mumtaz | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Goher_Mumtaz_Lead_Vocalist_and_Founder_of_Jal_The_Band_.jpg) |
| Gul Panra | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Ali_Zafar_and_Gul_Panra_2022.png) |
| Gulshan Grover | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Gulshan_Grover_(IIFA-2014-GreenCarpet2).jpg) |
| Guri | singer | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Guri_on_set_of_Lover_Movie.jpg) |
| Gurnam Bhullar | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Gurnam_Bhullar.jpg) |
| Gurshabad | singer | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Savi_B-115.jpg) |
| Guru Randhawa | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Guru_Randhawa_in_2019.jpg) |
| H Dhami | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:H-Dhami.JPG) |
| Hadiqa Kiani | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Hadiqa_Kiani_Pakistani_Singer.jpg) |
| Happy Raikoti | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Happy_Raikoti.jpg) |
| Harjit Harman | singer | No verified free portrait | — | — |
| Himesh Reshammiya | singer | Approved | PD | [source](https://commons.wikimedia.org/wiki/File:Himesh.jpg) |
| Hrithik Roshan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Hrithik_at_Rado_launch.jpg) |
| Humera Channa | singer | Conditional | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Humera_Channa_2019.png) |
| Imran Khan | singer | Conditional | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Imran_Khan_Singer.jpg) |
| Inayat Hussain Bhatti | singer | Conditional | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Late_.aniyat_husain_bhatti.jpg) |
| Irrfan Khan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:IrrfanKhan.jpg) |
| Jackie Shroff | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Jackie.Shroff.jpg) |
| Jaggesh | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Jaggesh_(2).jpg) |
| Jagjit Singh | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Jagjit_Singh_%28Ghazal_Maestro%29.jpg) |
| Jasmine Sandlas | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Jasmine_Sandlas.jpg) |
| Javed Ali | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Javed_Ali_graces_musical_concert_%E2%80%98Rehmatein-3%E2%80%99.jpg) |
| Jaz Dhami | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Jaz_Dhami_Punjabi_Artist.jpg) |
| Jean-Claude Van Damme | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Jean_claude_Van_Damme_sur_le_tournage_de_JCVD_en_octobre_2007.jpg) |
| Jim Carrey | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Jim_Carrey.jpg) |
| Jimmy Carr | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Jimmy_Carr,_2015-04-13_3_(crop).jpg) |
| John Abraham | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:John_at_Trailor_launch_of_%27Shootout_At_Wadala%27.jpg) |
| Johnny Lever | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Bollywood_Actor_Johnny_Lever_by_Creativo_Camaal_of_Lens_Naayak_Photography_Mumbai_India.jpg) |
| Johnny Walker | actor | No verified free portrait | — | — |
| K. S. Chithra | singer | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Melody_Queen_of_Indian_Cinema_Dr._K_S_Chithra.jpg) |
| Kader Khan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Kader_Khan_2012.jpg) |
| Kavita Krishnamurti | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Kavitha_Krishnamurty_DSC_0510.JPG) |
| Kay Kay Menon | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Kay_Kay_Act.JPG) |
| Kishore Kumar | singer | Conditional | GODL-India | [source](https://commons.wikimedia.org/wiki/File:Kishore_Kumar_2016_postcard_of_India_(cropped).jpg) |
| KK | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:KK_(124)_(headshot).jpg) |
| Komal Rizvi | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Komal_Rizvi_.jpeg) |
| Kumar Sanu | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Kumar_sanu_3_idiots.jpg) |
| Kunal Ganjawala | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:KunalGanjawala.jpg) |
| Kunal Khemu | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Kunal_Khemu.jpg) |
| Labh Janjua | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Labh_Janjua_%28cropped%29.jpg) |
| Lakhwinder Singh | singer | No verified free portrait | — | — |
| Lata Mangeshkar | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Lata_Mangeshkar.jpg) |
| Lehmber Hussainpuri | singer | No verified free portrait | — | — |
| Lokesh | actor | Conditional | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Lokesh.jpg) |
| Louis Theroux | actor | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Louis_Theroux_crop.jpg) |
| Lucky Ali | singer | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Lucky_Ali_at_a_concert_in_Goa.jpg) |
| Malkit Singh | singer | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Malkit_Singh_(cropped).jpg) |
| Maninder Buttar | singer | No verified free portrait | — | — |
| Manmohan Waris | singer | No verified free portrait | — | — |
| Mehmood | actor | No verified free portrait | GODL-India | [source](https://commons.wikimedia.org/wiki/File:Actor_Mehmood_Ali_2013_stamp_of_India.jpg) |
| Michael McIntyre | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Michael_McIntyre.jpg) |
| Michael Moore | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Michael_Moore_(2).jpg) |
| Mickey Singh | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Mickey_still.jpg) |
| Micky Flanagan | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Micky_Flanagan.jpg) |
| Miss Pooja | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Miss_Pooja_%40_Canada%27s_Wonderland_%282009-08-29%29.jpg) |
| Mithun Chakraborty | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Mitun-chakraborty_(cropped).jpg) |
| Mohammed Aziz | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Mohammad_Aziz_interview.png) |
| Mohammed Rafi | singer | Conditional | GODL-India | [source](https://commons.wikimedia.org/wiki/File:Mohammed_Rafi_2016_postcard_of_India_crop-flip.jpg) |
| Mohit Chauhan | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Mohit_Chauhan_performing_at_Alcheringa%2713.jpg) |
| Momina Mustehsan | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Momina_Mustehsan_at_New_Islamabad_Airport_(cropped).jpg) |
| Mukesh | singer | Approved | PD | [source](https://commons.wikimedia.org/wiki/File:Mukesh_Indian_Singer.jpg) |
| Munawar Zarif | actor | Approved | PD-Pakistan-US-1996 | [source](https://commons.wikimedia.org/wiki/File:Munawar_Zarif_Ajj_Da_Mehinwal.jpg) |
| Mustafa Zahid | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Mustafa_Zahid_at_Bradfort.jpg) |
| Nabeel Shaukat Ali | singer | No verified free portrait | — | — |
| Nachhatar Gill | singer | No verified free portrait | — | — |
| Nakash Aziz | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Nakash_Aziz.jpg) |
| Nana Patekar | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Nana_Patekar_2025.jpg) |
| Naseebo Lal | singer | Conditional | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Naseebo_Lal_2017_(2).png) |
| Naseeruddin Shah | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Naseeruddin_Shah_at_IFP_Season_9.jpg) |
| Navv Inder | singer | No verified free portrait | — | — |
| Nawazuddin Siddiqui | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Nawazuddin_Siddiqui_at_IFFK_2021_4_(cropped).jpg) |
| Nayyara Noor | singer | No verified free portrait | — | — |
| Neha Kakkar | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Neha_Kakkar_snapped_at_Miss_World_2024_at_Jio_Convention_Centre,_BKC.jpg) |
| Noor Jehan | singer | Conditional | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Noor_Jehan_%26_Lata_Mangeshkar.jpg) |
| Nusrat Fateh Ali Khan | singer | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Nusrat_Fateh_Ali_Khan_(1948-1997).jpg) |
| Om Puri | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:OmPuriSept10TIFF.jpg) |
| Palak Muchhal | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Palak_Muchhal_filmfare.jpg) |
| Pammi Bai | singer | No verified free portrait | — | — |
| Pankaj Tripathi | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Pankaj_Tripathi_World_Premiere_Newton_Zoopalast_Berlinale_2017_06.jpg) |
| Pankaj Udhas | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Pankaj_Udhas_still4.jpg) |
| Paresh Rawal | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Paresh_Rawal_still4.jpg) |
| Pav Dharia | singer | No verified free portrait | — | — |
| Peter Kay | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Peter_Kay_comedy_masterclass_at_University_of_Salford_12_December_2012.jpg) |
| Prajwal Devaraj | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Prajwal_Devaraj_(2015).jpg) |
| Preet Harpal | singer | Conditional | CC-BY-4.0 | [source](https://commons.wikimedia.org/wiki/File:Preet_Harpal.jpg) |
| Prem Chopra | actor | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Prem_Chopra_speaking.jpg) |
| Prem Dhillon | singer | Conditional | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Prem_Dhillon_Majha_Block.jpg) |
| Quentin Tarantino | actor | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Quentin_Tarantino_by_Gage_Skidmore.jpg) |
| Qurat-ul-Ain Balouch | singer | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Qurat-ul-Ain_Balouch.jpg) |
| Rahat Fateh Ali Khan | singer | Approved | PD-Bangladesh-PID | [source](https://commons.wikimedia.org/wiki/File:Rahat_Fateh_Ali_Khan_in_2024.jpg) |
| Rahim Shah | singer | No verified free portrait | — | — |
| Rahul Dev | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Rahul_Dev_(cropped).jpg) |
| Raj Babbar | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Raaj_Babbar.jpg) |
| Raj Brar | singer | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Raj_Brar.jpg) |
| Rajendra Kumar | actor | Conditional | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Rajendra_Kumar.jpg) |
| Rajesh Khanna | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Rajesh_Khanna_Profile.jpg) |
| Rajkummar Rao | actor | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Actor_Rajkummar_Rao.jpg) |
| Ramesh Aravind | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Ramesh_Aravind.jpg) |
| Ranbir Kapoor | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Ranbir_Kapoor_in_April_2024.jpg) |
| Randeep Hooda | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Randeep_Jism_2_conference.jpg) |
| Ranjit Bawa | singer | Conditional | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Ranjit_Bawa_lnterview.jpg) |
| Ranveer Singh | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Ranveer_Singh_in_2023_(1)_(cropped).jpg) |
| Ravinder Grewal | singer | Approved | CC-BY-4.0 | [source](https://commons.wikimedia.org/wiki/File:Ravinder_Singh_Grewal.jpg) |
| Rishi Kapoor | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Rishi_Kapoor.jpg) |
| Runa Laila | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Runa_Laila_2023.jpg) |
| Saif Ali Khan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Saif_Ali_Khan_promoting_Jawaani_Jaaneman.jpeg) |
| Salman Khan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Salman_Khan_snapped_at_the_Angry_Young_Men_trailer_launch.jpg) |
| Sanjay Dutt | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Sanjay_dutt_department.jpg) |
| Sanjay Mishra | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Sanjay_Mishra_Guthlee-Ladoo_(cropped).jpg) |
| Sarbjit Cheema | singer | Conditional | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Sarabjit_Cheema_%28cropped%29.jpg) |
| Sardool Sikander | singer | No verified free portrait | — | — |
| Satinder Sartaaj | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Satinder_Sartaaj_%2818th_IIFA_Awards%2C_2017%29.jpg) |
| Satwinder Bitti | singer | No verified free portrait | — | — |
| Shaan | singer | Approved | CC-BY-4.0 | [source](https://commons.wikimedia.org/wiki/File:Singer_Shantanu_Mukkerjee_alias_Shaan.jpg) |
| Shakti Kapoor | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:01_Shakti_Kapoor_2023_(profile).jpeg) |
| Shammi Kapoor | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Shammi_Kapoor_at_the_launch_of_Shammi_Kapoor_Unplugged.jpg) |
| Shankar Nag | actor | No verified free portrait | — | — |
| Sharman Joshi | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Sharman_Joshi_partyy.jpg) |
| Sharry Maan | singer | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Sharry_Mann-_41365566410_%28cropped%29.jpg) |
| Shatrughan Sinha | actor | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Shatrughan_Sinha_00451_(cropped).JPG) |
| Sheera Jasvir | singer | No verified free portrait | — | — |
| Shiva Rajkumar | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:ShivR_(cropped).jpg) |
| Shreya Ghoshal | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Shreya_ghoshal_song_saali_khushi_(cropped).jpg) |
| Sidhu Moose Wala | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Sidhu_Moose_Wala_during_the_shooting_of_his_film_Moosa_Jatt_%28cropped%29.jpg) |
| Sohail Khan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:SohailKhan.jpg) |
| Sonu Kakkar | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Sonu_Kakkar.jpg) |
| Sonu Sood | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:IIFA_2017_Green_Carpet_(35586582163).jpg) |
| Steven Seagal | actor | Approved | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Steven_Seagal_by_Gage_Skidmore.jpg) |
| Sudeep | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Sudeep_interview_TeachAIDS.jpg) |
| Sukhwinder Singh | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Sukhwinder_Singh_singer.jpg) |
| Sukshinder Shinda | singer | Conditional | CC-BY-SA-3.0 | [source](https://commons.wikimedia.org/wiki/File:Photoshute_pic_2014-05-08_11-52.jpg) |
| Sunanda Sharma | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Sunanda_Sharma_%28singer%29.jpg) |
| Sunidhi Chauhan | singer | Approved | CC-BY-4.0 | [source](https://commons.wikimedia.org/wiki/File:Sunidhi_Chauhan_performing_in_Delhi.jpg) |
| Suniel Shetty | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Sunil_Shetty.jpg) |
| Surjit Bindrakhia | singer | Conditional | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Surjit_Bindrakhia.jpg) |
| Surjit Khan | singer | Conditional | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Surjit_Khan_Singer.jpg) |
| Sushant Singh Rajput | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Sushant_Singh_Rajput,_IFFI_2017,_Goa,_India.jpg) |
| Tarsem Jassar | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Tarsem_Jassar.jpg) |
| Tiger Shroff | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Tiger_Shroff_in_2019.jpg) |
| Tom Hardy | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Tom_Hardy_(41869508740).jpg) |
| Tulsi Kumar | singer | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Tulsi_Kumar_in_Screen_Awards_2019_(5).jpg) |
| Tusshar Kapoor | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Tusshar_Kapoor_%28IIFA-2014-GreenCarpet2_%28306%29.jpg) |
| Udit Narayan | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Singer_Udit_Narayan.jpg) |
| Varun Dhawan | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Varun_Dhawan_at_Mehboob_Studios_in_2025_(cropped).jpg) |
| Vidyut Jammwal | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Vidyut_jamwal1.jpg) |
| Vinod Khanna | actor | Approved | CC-BY-3.0 | [source](https://commons.wikimedia.org/wiki/File:Vinod_Khanna_at_Esha_Deol%27s_wedding_at_ISCKON_temple_11_(cropped).jpg) |
| Vishal Dadlani | singer | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:VishalNH77.jpg) |
| Vishnuvardhan | actor | Approved | CC-BY-SA-4.0 | [source](https://commons.wikimedia.org/wiki/File:Vishnuvardhan_5.jpg) |
| Vivek Oberoi | actor | Approved | CC-BY-2.0 | [source](https://commons.wikimedia.org/wiki/File:Vivek_Oberoi_(2014)_01.jpg) |
| Will Ferrell | actor | Approved | CC-BY-SA-2.0 | [source](https://commons.wikimedia.org/wiki/File:Will_Ferrell_2013.jpg) |
| Yuvraj Hans | singer | Approved | CC0-1.0 | [source](https://commons.wikimedia.org/wiki/File:Yuvraj_and_Hans_Raj_Hans.jpg) |
