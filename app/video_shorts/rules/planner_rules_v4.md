# Planner Rules v4

Version: v4-2025-02-02

## Non-speech detection
Keywords:
- [music]
- music
- muzik
- alkis
- applause
- silence
- sessizlik
- kahkaha
- laughter
- noise
- background noise
- sfx
- sound effect
- efekt

Regex patterns:
- ^\s*\[music\]\s*$
- ^\s*\[applause\]\s*$
- ^\s*\[laughter\]\s*$
- ^\s*\[noise\]\s*$
- ^\s*\[silence\]\s*$
- ^\s*\[sfx\]\s*$
- ^\s*[\u266a\u266b]+\s*$

Min speech chars: 3
Max single word chars: 2

## Conjunction prefixes
- ama
- fakat
- cunku
- ve
- binaenaleyh
- lakin
- ancak

## Clip settings
Min clip seconds: 25.0
Max clip seconds: 60.0
Target clip count default: 8
Max overlap ratio: 0.85
Min gap seconds: 3.0
Max per label: 3

## QA settings
QA max question seconds: 10.0
QA min answer ratio: 2.0
QA keywords:
- soru
- soran
- dinleyici
- kardesimiz

## Allowed labels
- story
- qa
- tefsir
- principle
- other

## Few-shot examples

EXAMPLE 1 (video-1)
Segments subset for SHORT A (gold: idx 16-33):
idx-16 | 1:18.520 - 1:19.173 | Nebilerin,
idx-17 | 1:19.173 - 1:22.440 | Sıddıkların ağlayıp geçtikleri şu dünyada,
idx-18 | 1:22.500 - 1:27.520 | bu ağlamalar bir gülmeli hayatın tohumları mahiyetindedir.
idx-19 | 1:28.620 - 1:31.160 | Öbür alemde kesretler,
idx-20 | 1:31.160 - 1:36.240 | manalar, engeller, perdeler, hâiller mani olmadan,
idx-21 | 1:37.200 - 1:38.249 | Hammadun olarak,
idx-22 | 1:38.249 - 1:41.920 | Allah'a çok hamd eden bir cemaat olarak,
idx-23 | 1:41.920 - 1:47.920 | elinde hammadunun bayrağını taşıyan Hazreti Muhammed'in,
idx-24 | 1:47.940 - 1:48.436 | yani
idx-25 | 1:48.436 - 1:50.580 | Ahmed-i Mahmud-i Muhammed Mustafa'nın
idx-26 | 1:50.580 - 1:52.080 | sallallahu aleyhi ve sellem,
idx-27 | 1:52.080 - 1:53.820 | bayrağının altında toplanacak,
idx-28 | 1:53.820 - 2:00.020 | manisiz, perdesiz, hâilsiz,
idx-29 | 2:00.020 - 2:00.220 | bütün
idx-30 | 2:00.220 - 2:05.820 | güzelliklerin kaynağı olan Cenab-ı Hakk'ın cemalini müşahede edecek
idx-31 | 2:05.820 - 2:07.440 | ve kendimizden geçeceğiz.

Segments subset for SHORT B (gold: idx 108-116):
idx-108 | 7:29.380 - 7:30.560 | Her şeyi ona borçluyuz.
idx-109 | 7:31.400 - 7:35.540 | Binaenaleyh çok defa gölgeler arkasında yürüyen sizler.
idx-110 | 7:36.660 - 7:41.820 | Çok defa fani şahısların fani olmalarına işlerinizi bina eden sizler.
idx-111 | 7:41.820 - 7:51.160 | Ben biliyorum ki siz bütün bu gölgelerin önünde, bütün bu gölgelerin mihrabında, bütün bu gölgelerin minberinde
idx-112 | 7:52.200 - 7:54.060 | nazarınızda tek şey vardır.
idx-113 | 7:54.800 - 7:56.093 | O da Hz.
idx-114 | 7:56.093 - 7:56.956 | Muhammed Mustafa
idx-115 | 7:56.956 - 7:57.818 | sallallahu aleyhi
idx-116 | 7:57.818 - 7:58.680 | ve sellem'dir.

Gold output (2 short):
- {label:"story", start_idx:16, end_idx:33, why_selected:"Duygudan baslayip net bir manevi zirveye ulasan, tek parca tamamlanan bir dusunce akisi var."}
- {label:"principle", start_idx:108, end_idx:116, why_selected:"Net bir tez ile baslayip tek bir odakta (Hz. Muhammed) kapanan guclu bir vurgu yapiyor."}

EXAMPLE 2 (video-2)
Segments subset for SHORT A (gold: idx 1-8):
idx-1 | 0:06.040 - 0:13.060 | Şöyle de diyebiliriz. İnsanlık, iman, bu imandaki marifetle, o bilgiyle yeniden bir doğuşa ulaşmıştır. Bu doğuşu bize temin edene ruhumuz feda olsun. Bu doğuşu bize temin eden Hz. Muhammed
idx-2 | 0:13.060 - 0:15.868 | Mustafa'dır. Onun için bir şeye dikkatinizi çekeceğim. Hz. Muhammed'in veladeti aynı zamanda insanlığın yeniden veladetidir. Hz. Muhammed doğarken
idx-3 | 0:15.868 - 0:20.080 | insanlık yeniden bir kere daha doğmuştur.
idx-4 | 0:20.080 - 0:31.820 | Çölün dehşet engiz manzarası karşısında iki büklüm oluyorduk. Onun doğuşuyla gökten yere bir nur indi. Bütün varlık aydınlandı. Bu aydınlanmış iklimde biz de kendimizin yeniden doğduğumuzu gördük. O sayede yeniden bir kere daha varlığa erdik. Varlığa erdiren Sultanı
idx-5 | 0:32.560 - 0:33.268 | Zişan'a ruhlarımız feda olsun.
idx-6 | 0:33.268 - 0:38.226 | sana feda olsun. Ya Resulallah deyip sonra da dudaklarını yaladıkları Hz. Muhammed Mustafa sallallahu aleyhi ve sellem. Ruhlarımız ona feda olsun.
idx-7 | 0:38.226 - 0:38.934 | (devam)
idx-8 | 0:38.934 - 0:44.600 | (devam)

Segments subset for SHORT B (gold: idx 10-14):
idx-10 | 1:02.360 - 1:20.740 | Çölün dehşet engiz manzarası karşısında iki büklüm oluyorduk. Onun doğuşuyla gökten yere bir nur indi. Bütün varlık aydınlandı. Bu aydınlanmış iklimde biz de kendimizin yeniden doğduğumuzu gördük.
idx-11 | 1:21.720 - 1:37.740 | O sayede yeniden bir kere daha varlığa erdik. Varlığa erdiren Sultanı Zişan'a ruhlarımız feda olsun.
idx-12 | 1:37.740 - 1:49.454 | Bi ebi ente ve ummi anam babam tatlı canım sana feda olsun. Ya Resulallah deyip sonra da dudaklarını yaladıkları Hz.
idx-13 | 1:49.454 - 1:52.969 | Muhammed Mustafa sallallahu aleyhi ve sellem.
idx-14 | 1:52.969 - 1:55.311 | Ruhlarımız ona feda olsun.

Gold output (2 short):
- {label:"principle", start_idx:1, end_idx:8, why_selected:"Iman ve yeniden dogus temasini hizla kuruyor ve tek mesaj halinde toparliyor."}
- {label:"principle", start_idx:10, end_idx:14, why_selected:"Karanliktan nura gecis gibi net bir donusum duygusu verip guclu bir kapanis yapiyor."}

EXAMPLE 3 (video-3)
Segments subset for SHORT A (gold: idx 56-70):
idx-56 | 3:57.000 - 3:57.827 | Kızcağızım,
idx-57 | 3:57.827 - 3:58.653 | korkma,
idx-58 | 3:58.653 - 4:01.960 | Allah senin babanı zayi etmeyecektir.
idx-59 | 4:03.480 - 4:08.140 | Yerinizin idraki içindeyseniz ve kendinizden emin bulunuyorsanız,
idx-60 | 4:09.020 - 4:10.340 | Ben de aynı teminatı,
idx-61 | 4:11.740 - 4:17.920 | O bizim için tevekkülün de teslimiyetin de timsali olan o büyük zaata, itimat ve inkıyad ile
idx-62 | 4:19.980 - 4:22.160 | Aynı şeyi söyleyeyim.
idx-63 | 4:22.580 - 4:23.264 | Korkmayın,
idx-64 | 4:23.264 - 4:26.000 | Allah sizi zayi etmeyecektir.
idx-65 | 4:26.440 - 4:31.720 | Fakat ...
idx-66 | 4:31.720 - 4:34.720 | (devam)
idx-67 | 4:34.720 - 4:42.420 | (devam)
idx-68 | 4:42.420 - 4:46.960 | Herhangi bir çukura, herhangi bir deliğe düşme ihtimaliniz vardır.
idx-69 | 4:48.840 - 4:52.420 | Teminatınızı, teminat noktalarınızı bir kere daha kontrol ediniz.
idx-70 | 4:53.640 - 4:57.100 | Bir suvari gibi atınızın kolanlarına bir kere daha bakıveriniz.

Segments subset for SHORT B (gold: idx 75-87):
idx-75 | 5:07.400 - 5:10.260 | Allah'la münasebetiniz açısından neredesiniz?
idx-76 | 5:11.200 - 5:15.540 | Elem ye’ni lillezîne âmenû en tahşe‘a kulûbuhum li zikrillâh
idx-77 | 5:17.620 - 5:23.380 | Allah'ın bu kadar lütufları içinde, nurdan lütuflarını yarıp yarıp,
idx-78 | 5:24.120 - 5:26.700 | Hevenk hevenk nurdan lütufları içinde gezerken,
idx-79 | 5:27.540 - 5:30.060 | Kalbinizin yumuşayacağı an gelmedi mi diyor?
idx-80 | 5:31.920 - 5:35.800 | Başkaları için mukadder olan sû-i akıbet sizin için de mukadderdir.
idx-81 | 5:37.000 - 5:42.120 | Başkalarının zebil olarak döküldüğü çukurlara sizin de dökülmeniz mukadderdir.
idx-82 | 5:43.300 - 5:45.680 | Gelin bir kere daha söz verip Allah'a,
idx-83 | 5:46.300 - 5:49.580 | Başlattığınız şu ahdü peymana bir kere daha yeminde bulunalım.
idx-84 | 5:50.880 - 5:52.980 | Bu can bu uğurda bir kere daha diyelim.
idx-85 | 5:54.780 - 5:57.320 | Şu üç büyük beldenin insanı olarak,
idx-86 | 5:58.460 - 6:00.580 | Kendimizi bir kere daha kontrol edelim.
idx-87 | 6:03.060 - 6:04.580 | Kaybetmeyecek kazanacağız.

Gold output (2 short):
- {label:"qa", start_idx:56, end_idx:70, why_selected:"Teselli ile baslayip dinleyiciyi oz kontrol cagrisina tasiyan net bir akisi var."}
- {label:"qa", start_idx:75, end_idx:87, why_selected:"Soru ile gerilim kurup uyarilari ardarda getiriyor ve guclu bir final cumlesiyle kapaniyor."}
