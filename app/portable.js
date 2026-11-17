import { site } from './siteconfig.js'
import dompurify from 'dompurify';
import { marked } from 'marked';

// API proxys
const jwtEndpoint = "/app/jwt/get"; // Get JSON Web Token for Single Sign-On
const openaiChatCompletionsProxy = "/openai/v1/chat/completions";
const openaiModerationsProxy = "/openai/v1/moderations";
const openaiAudioTranscriptionsProxy = "/openai/v1/audio/transcriptions";
const geminiProxy = "/gemini/";
const googleTTSProxy = "/gtts/";
const elevenTTSProxy = [
  "wss://" + window.location.host + "/elevenlabs/",
  "/v1/text-to-speech/",
  "/stream-input?model_id=eleven_turbo_v2_5&output_format=pcm_22050&auto_mode=true&apply_text_normalization=off"
];
const microsoftTTSProxy = [
  "wss://" + window.location.host + "/mstts/",
  "/cognitiveservices/websocket/v1"
];
const grokChatCompletionsProxy = "/grok/v1/chat/completions"; // Grok-beta
const llamaChatCompletionsProxy = "/llama/v1/chat/completions"; // Local llama.cpp server proxy
const localWhisperCppProxy = "/whisper/"; // Local whisper.cpp server proxy


// Direct API endpoints
const openaiChatCompletionsEndpoint = "https://api.openai.com/v1/chat/completions";
const openaiModerationsEndpoint = "https://api.openai.com/v1/moderations";
const openaiAudioTranscriptionsEndpoint = "https://api.openai.com/v1/audio/transcriptions";
const geminiEndpoint = "https://generativelanguage.googleapis.com/v1beta/models/";
const googleTTSEndpoint = "https://eu-texttospeech.googleapis.com/v1beta1/text:synthesize";
const elevenTTSEndpoint = [
  "wss://api.elevenlabs.io/v1/text-to-speech/",
  "/stream-input?model_id=eleven_turbo_v2_5&output_format=pcm_22050&auto_mode=true&apply_text_normalization=off"
];
const microsoftTTSEndpoint = "wss://northeurope.tts.speech.microsoft.com/cognitiveservices/websocket/v1";
const grokChatCompletionsEndpoint = "https://api.x.ai/v1/chat/completions";


// I18n internationalization
const i18n = {

  'fi': {
    'Avatar': 'Hahmo', 'Camera': 'Kamera', 'Audio': 'Ääni', 'Manuscript': 'Käsikirjoitus',
    'Emotion': 'Tunne', 'Neutral': 'Perus', 'Happy': 'Ilo', 'Angry': 'Viha', 'Sad': 'Suru',
    'Fear': 'Pelko', 'Disgust': 'Inho', 'Love': 'Rakkaus', 'Sleep': 'Uni', 'Pose': 'Asento',
    'Action': 'Toiminta', 'Frame': 'Rajaus', 'Full': 'Kokovartalo', 'Upper': 'Yläosa',
    'Head': 'Pää', 'Director': 'Ohjaus', 'Pause': 'Pysäytyskuva', 'Panning': 'Panorointi',
    'Slow-motion': 'Hidastus', 'Ambience': 'Ambienssi', 'Silence': 'Hiljaisuus',
    'Session': 'Sessio', 'Theme': 'Teema', 'Location': 'Paikka', 'Voice': 'Istuva',
    'AI': 'Tekoäly', 'Emoji': 'Emojit', 'Title': 'Otsikko', 'System': 'Ohjeistus',
    'ai-system': 'Järjestelmäviesti.', 'ai-user1': 'Käyttäjän syöte #1',
    'ai-ai1': 'Tekoälyn vaste #1', 'ai-user2': 'Käyttäjän syöte #2',
    'ai-ai2': 'Tekoälyn vaste #2', 'Example1': 'Esim #1', 'Example2': 'Esim #2',
    'Dark': 'Tumma', 'Light': 'Vaalea', 'theme-wide': 'Laajakuva', 'theme-43': '4:3',
    'theme-landscape': 'Vaaka', 'theme-portrait': 'Pysty', 'Empty': 'Tyhjä',
    'Adjust': 'Säätö', 'Speech': 'Puhe', 'Silence': 'Hiljaisuus', 'Framing': 'Rajaus',
    'Mixer': 'Mikseri', 'Space': 'Tila', 'Dry': 'Suora', 'voice-test': 'Äänitesti',
    'Limits': 'Rajat', 'ai-stop': 'Stop', 'ai-stopword': 'Avainsana', 'ai-user': 'Käyttäjä',
    'ai-username': 'Nimi', 'input': 'Kirjoita viesti.', 'Name': 'Nimi', 'Language': 'Kieli',
    'en': 'English', 'fi': 'Finnish', 'words': 'sanaa', 'dialogs': 'sanomaa',
    'Manuscript': 'Käsikirjoitus', 'Exclude': 'Ohita', 'Italics': 'Kursiivi',
    'Code': 'Koodi', 'Light': 'Valo', 'LightAmbient': 'Ambientti',
    'LightDirect': 'Suunnattu', 'LightSpot': 'Spotti', 'theme-full': "Täysi",
    'lt': 'Liettua', 'test-sentence': "Kirjoita tähän testilause.", 'Mid': 'Keskiosa',
     'Gesture': 'Ele', 'PoseMovement': 'Liike', 'Visible': 'Näytä',
     'Helper': 'Apuri', 'Edit': 'Muokkaa', 'Script': 'Skripti', 'fr': "Ranska",
     'de': "Saksa",
     'BoneControl': 'Luun ohjaus', 'BoneControlOn': 'Päälle', 'BoneControlOff': 'Pois',
     'SelectBone': 'Valitse luu', 'Rotation': 'Kierto', 'Position': 'Sijainti', 'Scale': 'Mittakaava',
     'Reset': 'Nollaa', 'ResetAll': 'Nollaa kaikki',
     'DeltaL': 'Siirto paikall.', 'DeltaW': 'Siirto maailm.', 'Other': 'Muu',
     'Features': 'Ominaisuudet', 'DynamicBones': 'Dynaam. luut', 'Limits': 'Rajat',
     'Excludes': 'Poikkeukset', 'Pivots': 'Tukipisteet', 'Sensitivity': 'Herkkyys',
     'Movement': 'Liike', 'Type': 'Tyyppi', 'Stiffness': 'Jäykkyys', 'Data': 'Data'
   },

  'en': {
    'ai-system': 'System message.', 'ai-user1': 'User example #1',
    'ai-ai1': 'AI response #1', 'ai-user2': 'User example #2',
    'ai-ai2': 'AI response #2', 'Example1': 'Example-1', 'Example2': 'Example-2',
    'theme-wide': 'Widescreen', 'theme-43': '4:3', 'theme-landscape': 'Landscape',
    'theme-portrait': 'Portrait', 'voice-test': 'Speak', 'ai-stop': 'Stop',
    'ai-user': 'User', 'input': 'Message.', 'en': 'English', 'fi': 'Finnish',
    'ai-stopword': 'Word', 'ai-username': 'Name', 'LightAmbient': 'Ambient',
    'LightDirect': 'Direct', 'LightSpot': 'Spot', 'theme-full': "Fullscreen",
    'lt': 'Lithuanian', 'test-sentence': "Write your test sentence.",
     'PoseMovement': 'Movement', 'fr': "French", 'de': "German",
     'BoneControl': 'Bone Control', 'BoneControlOn': 'Enable', 'BoneControlOff': 'Disable',
     'SelectBone': 'Select Bone', 'Rotation': 'Rotation', 'Position': 'Position', 'Scale': 'Scale',
     'Reset': 'Reset', 'ResetAll': 'Reset All',
     'DeltaL': 'Delta Local', 'DeltaW': 'Delta World', 'Other': 'Other',
     'Features': 'Features', 'DynamicBones': 'Dynamic Bones', 'Limits': 'Limits',
     'Excludes': 'Excludes', 'Pivots': 'Pivots', 'Sensitivity': 'Sensitivity',
     'Movement': 'Movement', 'Type': 'Type', 'Stiffness': 'Stiffness', 'Data': 'Data'
   },

  'ru': {
    'Avatar': 'Аватар', 'Camera': 'Камера', 'Audio': 'Аудио', 'Manuscript': 'Сценарий',
    'Emotion': 'Эмоция', 'Neutral': 'Нейтральная', 'Happy': 'Радость', 'Angry': 'Злость', 'Sad': 'Грусть',
    'Fear': 'Страх', 'Disgust': 'Отвращение', 'Love': 'Любовь', 'Sleep': 'Сон', 'Pose': 'Поза',
    'Action': 'Действие', 'Frame': 'Кадрирование', 'Full': 'В полный рост', 'Upper': 'Верхняя часть',
    'Head': 'Голова', 'Director': 'Режиссёр', 'Pause': 'Пауза', 'Panning': 'Панорамирование',
    'Slow-motion': 'Замедление', 'Ambience': 'Атмосфера', 'Silence': 'Тишина',
    'Session': 'Сессия', 'Theme': 'Тема', 'Location': 'Место', 'Voice': 'Голос',
    'AI': 'ИИ', 'Emoji': 'Эмодзи', 'Title': 'Заголовок', 'System': 'Система',
    'ai-system': 'Системное сообщение.', 'ai-user1': 'Ввод пользователя #1',
    'ai-ai1': 'Ответ ИИ #1', 'ai-user2': 'Ввод пользователя #2',
    'ai-ai2': 'Ответ ИИ #2', 'Example1': 'Пример #1', 'Example2': 'Пример #2',
    'Dark': 'Тёмная', 'Light': 'Светлая', 'theme-wide': 'Широкоформатный', 'theme-43': '4:3',
    'theme-landscape': 'Альбомный', 'theme-portrait': 'Портретный', 'Empty': 'Пусто',
    'Adjust': 'Настройка', 'Speech': 'Речь', 'Framing': 'Кадрирование',
    'Mixer': 'Микшер', 'Space': 'Пространство', 'Dry': 'Прямой', 'voice-test': 'Тест голоса',
    'Limits': 'Ограничения', 'ai-stop': 'Стоп', 'ai-stopword': 'Ключевое слово', 'ai-user': 'Пользователь',
    'ai-username': 'Имя', 'input': 'Введите сообщение.', 'Name': 'Имя', 'Language': 'Язык',
    'en': 'Английский', 'fi': 'Финский', 'ru': 'Русский', 'words': 'слов', 'dialogs': 'сообщений',
    'Exclude': 'Исключить', 'Italics': 'Курсив',
    'Code': 'Код', 'LightAmbient': 'Фоновый',
    'LightDirect': 'Направленный', 'LightSpot': 'Точечный', 'theme-full': 'Полноэкранный',
    'lt': 'Литовский', 'test-sentence': 'Напишите тестовое предложение.', 'Mid': 'Средняя часть',
    'Gesture': 'Жест', 'PoseMovement': 'Движение', 'Visible': 'Видимый',
    'Helper': 'Помощник', 'Edit': 'Редактировать', 'Script': 'Скрипт', 'fr': 'Французский',
    'de': 'Немецкий',
    'BoneControl': 'Управление костями', 'BoneControlOn': 'Включить', 'BoneControlOff': 'Выключить',
    'SelectBone': 'Выбрать кость', 'Rotation': 'Вращение', 'Position': 'Позиция', 'Scale': 'Масштаб',
    'Reset': 'Сбросить', 'ResetAll': 'Сбросить все',
    'DeltaL': 'Смещение лок.', 'DeltaW': 'Смещение мир.', 'Other': 'Другое',
    'Features': 'Функции', 'DynamicBones': 'Динам. кости', 'Limits': 'Ограничения',
    'Excludes': 'Исключения', 'Pivots': 'Опорные точки', 'Sensitivity': 'Чувствительность',
    'Movement': 'Движение', 'Type': 'Тип', 'Stiffness': 'Жёсткость', 'Data': 'Данные'
  }

}

// i18n
// Default UI language is English

function i18nWord(w,l) {
  l = l || cfg('theme-lang') || 'en';
  return (( i18n[l] && i18n[l][w] ) ? i18n[l][w] : w);
}

function i18nTranslate(l) {
  l = l || cfg('theme-lang') || 'en';

  // Text
  d3.selectAll("[data-i18n-text]").nodes().forEach( n => {
    const e = d3.select(n);
    e.text( i18nWord(e.attr("data-i18n-text"),l ) );
  });

  // Title
  d3.selectAll("[data-i18n-title]").nodes().forEach( n => {
    const e = d3.select(n);
    e.attr( 'title', i18nWord( e.attr("data-i18n-title"),l ) );
  });

  // Placeholder
  d3.selectAll("[data-i18n-placeholder]").nodes().forEach( n => {
    const e = d3.select(n);
    e.attr( 'placeholder', i18nWord( e.attr("data-i18n-placeholder"),l) );
  });

  // Site
  d3.selectAll("[data-i18n-site]").nodes().forEach( n => {
    const e = d3.select(n);
    const label = e.attr("data-i18n-site");
    const [section, ...rest] = label.split('-');
    const item = rest.join('-');
    let text = item;
    if ( site[section] && site[section][item] && site[section][item][l] ) {
      text = site[section][item][l];
    }
    e.text( text );
  });
}

// Markdown configuration
const markedOptions = { gfm: true, breaks: true };

// Open AI configuration
let aiController = null;

// ElevenLabs configuration
const elevenBOS = {
  "text": " ",
  "voice_settings": { "stability": 0.8, "similarity_boost": true },
  "generation_config": {
    "chunk_length_schedule": [500,500,500,500]
  }
};
let elevenSocket = null;
let elevenInputMsgs = null;
let elevenOutputMsg = null;
let elevenOnProcessed = null;

// JSON Web Token (JWT)
let jwtExpires = 0;
let jwt = '';
let jwtEndpointAvailable = true;
let jwtInflight = null;

// Get JSON Web Token
async function jwtGet() {
  const limit = Math.round(Date.now() / 1000) + 300;
  if ( jwtExpires < limit ) {
    // Don't try again if endpoint is not available
    if ( !jwtEndpointAvailable ) {
      return jwt.slice();
    }
    // Singleflight: при параллельных вызовах все ждут один и тот же fetch,
    // иначе на старте страницы прилетает по 5-10 одинаковых 404 в консоль.
    if ( jwtInflight ) {
      try { await jwtInflight; } catch(e) {}
      return jwt.slice();
    }
    jwtInflight = (async () => {
      try {
        const resp = await fetch( jwtEndpoint, { cache: "no-store" } );
        if ( !resp.ok ) throw new Error('HTTP ' + resp.status);
        const ct = resp.headers.get('content-type') || '';
        if ( !ct.includes('json') ) throw new Error('Non-JSON response');
        const o = await resp.json();
        if ( o && o.jwt ) {
          const b64Url = o.jwt.split('.')[1];
          const b64 = b64Url.replace(/-/g, '+').replace(/_/g, '/');
          const s = decodeURIComponent( window.atob(b64).split('').map( (c) => {
            return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
          }).join(''));
          const p = JSON.parse(s);
          jwtExpires = (p && p.exp) ? p.exp : 0;
          jwt = o.jwt;
        } else {
          jwt = '';
          jwtExpires = 0;
        }
      } catch(e) {
        jwtEndpointAvailable = false;
        jwt = '';
        jwtExpires = 0;
      }
    })();
    try { await jwtInflight; } finally { jwtInflight = null; }
  }
  return jwt.slice();
}

// Speak using ElevenLabs
async function elevenSpeak(s,node=null) {
  if ( !elevenSocket ) {
    // Temporary reservation of WebSocket connection
    elevenSocket = { readyState: 0 };

    // Temporary stack of message until the connection is established
    elevenInputMsgs = [
      elevenBOS,
      {
        "text": s,
        "try_trigger_generation": false,
        "flush": true
      }
    ];

    // Endpoint/proxy and authentication
    let url;
    const apikey = d3.select("#apikey-eleven").property("value");
    if ( apikey ) {
      url = elevenTTSEndpoint[0];
      url += cfg('voice-eleven-id');
      url += elevenTTSEndpoint[1];
      elevenInputMsgs[0]["xi_api_key"] = apikey;
    } else {
      url = elevenTTSProxy[0];
      url += await jwtGet();
      url += elevenTTSProxy[1];
      url += cfg('voice-eleven-id');
      url += elevenTTSProxy[2];
    }

    // Make the connection
    elevenSocket = new WebSocket(url);

    // Connection opened
    elevenSocket.onopen = function (event) {
      elevenOutputMsg = null;
      while (elevenInputMsgs.length > 0) {
        elevenSocket.send(JSON.stringify(elevenInputMsgs.shift()));
      }
    }

    // New message received
    elevenSocket.onmessage = function (event) {
      const r = JSON.parse(event.data);

      // Speak audio
      if ( (r.isFinal || r.normalizedAlignment) && elevenOutputMsg ) {
        head.speakAudio( elevenOutputMsg, { lipsyncLang: cfg('voice-lipsync-lang') }, node ? addText.bind(null,node) : null );
        if ( elevenOnProcessed ) {
          elevenOnProcessed();
        }
        elevenOutputMsg = null;
      }

      if ( !r.isFinal ) {
        // New part
        if ( r.alignment ) {
          elevenOutputMsg = { audio: [], words: [], wtimes: [], wdurations: [] };

          // Parse chars to words
          let word = '';
          let time = 0;
          let duration = 0;
          for( let i=0; i<r.alignment.chars.length; i++ ) {
            if ( word.length === 0 ) time = r.alignment.charStartTimesMs[i];
            if ( word.length && r.alignment.chars[i] === ' ' ) {
              elevenOutputMsg.words.push( word );
              elevenOutputMsg.wtimes.push(time);
              elevenOutputMsg.wdurations.push(duration);
              word = '';
              duration = 0;
            } else {
              duration += r.alignment.charDurationsMs[i];
              word += r.alignment.chars[i];
            }
          }
          if ( word.length ) {
            elevenOutputMsg.words.push(word);
            elevenOutputMsg.wtimes.push(time);
            elevenOutputMsg.wdurations.push(duration);
          }
        }

        // Add audio content to message
        if ( r.audio &&  elevenOutputMsg ) {
          elevenOutputMsg.audio.push( head.b64ToArrayBuffer( r.audio ) );
        }
      }
    };

    // Error
    elevenSocket.onerror = function (error) {
      if ( elevenOnProcessed ) elevenOnProcessed();
      console.error(`WebSocket Error: ${error}`);
    };

    // Connection closed
    elevenSocket.onclose = function (event) {
      if ( elevenOnProcessed ) elevenOnProcessed();
      if ( event.wasClean) {
        // console.info(`Connection closed cleanly, code=${event.code}, reason=${event.reason}`);
      } else {
        console.warn('Connection died');
      }
      elevenSocket = null;
    };
  } else {
    let msg = {
      "text": s
    };
    if ( s.length ) {
      msg["try_trigger_generation"] = false;
      msg["flush"] = true;
    }
    if (elevenSocket.readyState === 1) { // OPEN
      elevenSocket.send(JSON.stringify(msg))
    } else if ( elevenSocket.readyState === 0 ) { // CONNECTING
      elevenInputMsgs.push(msg);
    }
  }
}

// Speak using Silero TTS (local server)
// Silero TTS — accumulate text during stream, speak only when done
let sileroAbortController = null;
let sileroSpeaking = false;
let sileroAccumulatedText = '';
let sileroNode = null;

async function sileroSpeak(s, node=null) {
  if ( !s || !s.trim() ) return;

  console.log('[Silero] Speaking:', s.substring(0, 30));

  if (sileroAbortController) {
    sileroAbortController.abort();
  }
  sileroAbortController = new AbortController();

  const textToSpeak = s.trim();

  try {
    // voice-silero-url — полный URL до /api/tts (или пусто — берём свой сервер)
    let sileroUrl = cfg('voice-silero-url') || (location.origin + '/api/tts');
    // Если URL не заканчивается на /api/tts, добавляем (для обратной совместимости)
    if (!/\/api\/tts\/?$/.test(sileroUrl)) {
      sileroUrl = sileroUrl.replace(/\/+$/, '') + '/api/tts';
    }
    const speaker = avatarVoice();
    const rate = avatarRate();
    const pitch = avatarPitch();
    const params = new URLSearchParams({
      text: textToSpeak,
      speaker: speaker,
      sr: '48000',
      rate: rate,
      pitch: pitch
    });
    const url = sileroUrl + '?' + params.toString();
    console.log('[Silero] URL:', url);

    sileroSpeaking = true;
    const res = await fetch(url, { signal: sileroAbortController.signal });
    if ( !res.ok ) {
      console.error('Silero TTS error:', res.status, await res.text());
      sileroSpeaking = false;
      return;
    }

    const arrayBuffer = await res.arrayBuffer();
    sileroSpeaking = false;
    console.log('[Silero] Audio received:', arrayBuffer.byteLength, 'bytes');

    if (head && head.audioCtx) {
      try {
        const audioBuffer = await head.audioCtx.decodeAudioData(arrayBuffer);
        const source = head.audioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(head.audioCtx.destination);
        source.start(0);

        if (head.mtAvatar) {
          source.onended = () => {
            try { head.resetMouth(); } catch(e) {}
          };
          animateMouthDuringPlayback(head, audioBuffer.duration);
        }
      } catch(e) {
        console.error('Silero playback error:', e);
      }
    }
  } catch(e) {
    sileroSpeaking = false;
    if (e.name !== 'AbortError') {
      console.error('Silero error:', e);
    }
  }
}

// Reset silero — call before new AI request
function sileroReset() {
  if (sileroAbortController) {
    sileroAbortController.abort();
    sileroAbortController = null;
  }
  sileroSpeaking = false;
  sileroAccumulatedText = '';
  sileroNode = null;
}

// Silero through talkinghead.mjs
async function sileroSpeakViaHead(text, node) {
  if (!head || !text || !text.trim()) return;
  /* Артикуляция считается по языку синтеза: русский голос с английской
     раскладкой липсинка даёт неверные формы рта. */
  const lang = cfg('voice-silero-lang') || 'ru';
  await head.speakText(text.trim(), {
    lipsyncLang: lang,
    ttsVoice: avatarVoice(),
    ttsLang: lang,
    ttsRate: parseFloat(avatarRate()) || 1.0,
    ttsPitch: parseFloat(avatarPitch()) || 0
  }, null, []);
}

// Simple mouth animation during TTS playback
function animateMouthDuringPlayback(head, duration) {
  const startTime = Date.now();
  const interval = setInterval(() => {
    const elapsed = (Date.now() - startTime) / 1000;
    if ( elapsed >= duration ) {
      clearInterval(interval);
      try { head.resetMouth(); } catch(e) {}
      return;
    }
    // Random mouth movement
    try {
      const openness = Math.random() * 0.5;
      if ( head.mtAvatar['jawOpen'] ) {
        head.mtAvatar['jawOpen'].value = openness;
        head.mtAvatar['jawOpen'].needsUpdate = true;
      }
    } catch(e) {}
  }, 80);
}

// Speak using Microsoft
let microsoftSynthesizer = null;
const microsoftQueue= [];

async function microsoftSpeak(s, node=null, onprocessed=null) {

  if ( s === null ) {
    microsoftQueue.push( null );
  } else {

    // Voice config
    const id = cfg("voice-microsoft-id");
    const e = d3.select("[data-voice-microsoft-id='" + id + "']");
    const lang = e.attr("data-voice-microsoft-lang");

    // SSML
    const ssml = "<speak version='1.0' " +
      "xmlns:mstts='http://www.w3.org/2001/mstts' " +
      "xml:lang='" + lang + "'>" +
      "<voice name='" + id + "'>" +
      "<mstts:viseme type='redlips_front'/>" +
      s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;') +
      "</voice>" +
      "</speak>";

    microsoftQueue.push( {
      ssml: ssml,
      node: node,
      onprocessed: onprocessed,
      speak: {
        audio: [], words: [], wtimes: [], wdurations: [],
        visemes: [], vtimes: [], vdurations: [], markers: [], mtimes: []
      }
     } );
  }

  // If this was the first item, start the process
  if ( microsoftQueue.length === 1 ) {
    microsoftProcessQueue();
  }
}

async function microsoftProcessQueue() {

  if ( microsoftQueue.length ) {

    const job = microsoftQueue[0];

    if ( job === null ) {
      microsoftQueue.shift();
      if ( microsoftQueue.length === 0 &&  microsoftSynthesizer ) {
        microsoftSynthesizer.close();
        microsoftSynthesizer = null;
      }
    } else {

      // If we do not have a speech synthesizer, create a new
      if ( !microsoftSynthesizer ) {

        // Speech SDK больше не грузится вместе с рабочим столом (см.
        // desktop.html): подтягиваем его при первом реальном синтезе.
        if ( !window.SpeechSDK && typeof window.loadSpeechSdk === "function" ) {
          try { await window.loadSpeechSdk(); } catch (e) { /* ниже покажет ошибку */ }
        }
        if ( !window.SpeechSDK ) {
          throw new Error("Speech SDK не загрузился");
        }

        // Create a new speech synthesizer
        let endpoint;
        let config;
        const apikey = d3.select("#apikey-microsoft").property("value");
        if ( apikey ) {
          endpoint = microsoftTTSEndpoint;
          config = window.SpeechSDK.SpeechConfig.fromEndpoint(endpoint, apikey);
        } else {
          endpoint = microsoftTTSProxy[0] + await jwtGet() + microsoftTTSProxy[1];
          config = window.SpeechSDK.SpeechConfig.fromEndpoint(endpoint);
        }
        config.setProperty("SpeechServiceConnection_Endpoint",endpoint);
        config.speechSynthesisOutputFormat = window.SpeechSDK.SpeechSynthesisOutputFormat.Raw22050Hz16BitMonoPcm;
        config.setProperty(window.SpeechSDK.PropertyId.SpeechServiceResponse_RequestSentenceBoundary, "true");
        microsoftSynthesizer = new window.SpeechSDK.SpeechSynthesizer(config,null);

        // Viseme conversion from Microsoft to Oculus
        // TODO: Check this conversion again!
        const visemeMap = [
          "sil", 'aa', 'aa', 'O', 'E', // 0 - 4
          'E', 'I', 'U', 'O', 'aa', // 5 - 9
          'O', 'I', 'kk', 'RR', 'nn', // 10 - 14
          'SS', 'SS', 'TH', 'FF', 'DD', // 15 - 19
          'kk', 'PP' // 20 - 21
        ];

        // Process visemes
        microsoftSynthesizer.visemeReceived = function(s, e) {
          if ( microsoftQueue[0] && microsoftQueue[0].speak ) {
            const o = microsoftQueue[0].speak;
            const viseme = visemeMap[e.visemeId];
            const time = e.audioOffset / 10000;

            // Calculate the duration of the previous viseme
            if ( o.vdurations.length ) {

              if ( o.visemes[ o.visemes.length-1 ] === 0 ) {
                o.visemes.pop();
                o.vtimes.pop();
                o.vdurations.pop();
              } else {
                // Remove silence
                o.vdurations[ o.vdurations.length-1 ] = time - o.vtimes[ o.vdurations.length-1 ];
              }
            }



            // Add this viseme
            o.visemes.push( viseme );
            o.vtimes.push( time );
            o.vdurations.push( 75 ); // Duration will be fixed when the next viseme is received
          }
        };

        // Process word boundaries and punctuations
        microsoftSynthesizer.wordBoundary = function (s, e) {
          if ( microsoftQueue[0] && microsoftQueue[0].speak ) {
            const o = microsoftQueue[0].speak;
            const word = e.text;
            const time = e.audioOffset / 10000;
            const duration = e.duration / 10000;

            if ( e.boundaryType === "PunctuationBoundary" && o.words.length ) {
              o.words[ o.words.length-1 ] += word;
            } else if ( e.boundaryType === "WordBoundary" || e.boundaryType === "PunctuationBoundary" ) {
              o.words.push( word );
              o.wtimes.push( time );
              o.wdurations.push( duration );
            } else if ( e.boundaryType === "SentenceBoundary" ) {
              if ( time > 500 ) {
                o.markers.push( () => { head.lookAtCamera(500); });
                o.mtimes.push( time - 500 );
              }
            }
          }
        };

      }

      // Speak the SSML
      microsoftSynthesizer.speakSsmlAsync(job.ssml,
        function (result) {
          if ( microsoftQueue[0] && microsoftQueue[0].speak ) {
            if (result.reason === window.SpeechSDK.ResultReason.SynthesizingAudioCompleted) {
              const job = microsoftQueue[0];
              job.speak.audio.push( result.audioData );
              head.speakAudio(job.speak, {}, job.node ? addText.bind(null,job.node) : null );
              if ( job.onprocessed ) job.onprocessed();
            }
            microsoftQueue.shift();
            microsoftProcessQueue();
          }
        }, function (err) {
          if ( job.onprocessed ) job.onprocessed();
          console.log(err);
          microsoftQueue.shift();
          microsoftProcessQueue();
        }
      );

    }
  }
}

// Whisper MP3
let whisperAudio = null;
let whisperLipsyncLang = 'en';

async function whisperLoadMP3(file) {

  if ( cfg("whisper-type") === "openai" ) {
    try {
      d3.select("#playmp3").classed("disabled", true);

      const form = new FormData();
      form.append("file", file);
      form.append("model", "whisper-1");
      form.append("response_format", "verbose_json" );
      form.append("prompt","[The following is a full verbatim transcription without additional details, comments or emojis:]");
      form.append("timestamp_granularities[]", "word" );
      form.append("timestamp_granularities[]", "segment" );

      // Endpoint/proxy and authentication
      let url;
      let headers = {};
      const apikey = d3.select("#apikey-openai").property("value");
      if ( apikey ) {
        url = openaiAudioTranscriptionsEndpoint;
        headers["Authorization"] = "Bearer " + apikey;
      } else {
        url = openaiAudioTranscriptionsProxy;
        headers["Authorization"] = "Bearer " + await jwtGet()
      }

      const response = await fetch( url , {
        method: "POST",
        body: form,
        headers: headers
      });
      if ( response.ok ) {
        const json = await response.json();
        d3.select("#jsonmp3").property("value", JSON.stringify(json) );

        // Fetch audio
        if ( json.words && json.words.length ) {
          var reader = new FileReader();
          reader.readAsArrayBuffer(file);
          reader.onload = async readerEvent => {
            let arraybuffer = readerEvent.target.result;
            let audiobuffer = await head.audioCtx.decodeAudioData(arraybuffer);

            // Set lip-sync language
            whisperLipsyncLang = json.language.substring(0,2);

            // Add words to the audio object
            whisperAudio = {
              audio: audiobuffer,
              words: [],
              wtimes: [],
              wdurations: [],
              markers: [],
              mtimes: []
            };
            json.words.forEach( x => {
              // Word
              whisperAudio.words.push( x.word );

              // Starting time
              let t = 1000 * x.start;
              if ( t > 150 ) t -= 150;
              whisperAudio.wtimes.push( t );

              // Duration
              let d = 1000 * (x.end - x.start);
              if ( d > 20 ) d -= 20;
              whisperAudio.wdurations.push( d  );
            });

            // Add timed callback markers to the audio object
            const startSegment = async () => {
              // Look at the camera
              head.lookAtCamera(500);
              head.speakWithHands();
            };
            json.segments.forEach( x => {
              if ( x.start > 2 && x.text.length > 10 ) {
                whisperAudio.markers.push( startSegment );
                whisperAudio.mtimes.push( 1000 * x.start - 1000 );
              }
            });

            d3.select("#playmp3").classed("disabled", false);
          }
        }

      } else {
        d3.select("#jsonmp3").property("value", 'Error: ' + response.status + ' ' + response.statusText );
        console.log(response);
      }

    } catch (error) {
      console.log(error);
    }
  } else if ( cfg("whisper-type") === "local" ) {
    try {
      d3.select("#playmp3").classed("disabled", true);

      const form = new FormData();
      form.append("file", file);
      form.append("temperature","0.0");
      form.append("temperature_inc", "0.2");
      form.append("response_format", "verbose_json");
      const response = await fetch( localWhisperCppProxy + 'inference' , {
        method: "POST",
        body: form,
        headers: {
          "Authorization": "Bearer " + await jwtGet()
        }
      });
      if ( response.ok ) {
        const json = await response.json();
        d3.select("#jsonmp3").property("value", JSON.stringify(json) );

        // Fetch audio
        if ( json.segments && json.segments.length ) {
          var reader = new FileReader();
          reader.readAsArrayBuffer(file);
          reader.onload = async readerEvent => {
            let arraybuffer = readerEvent.target.result;
            let audiobuffer = await head.audioCtx.decodeAudioData(arraybuffer);

            // Set lip-sync language
            whisperLipsyncLang = json.language.substring(0,2);

            // Add words to the audio object
            whisperAudio = {
              audio: audiobuffer,
              words: [],
              wtimes: [],
              wdurations: [],
              markers: [],
              mtimes: []
            };

            // Add timed callback markers to the audio object
            const startSegment = async () => {
              // Look at the camera
              head.lookAtCamera(500);
              head.speakWithHands();
            };

            json.segments.forEach( (x,i) => {
              if ( x.words && x.words.length ) {

                // Look at camera
                if ( x.start > 2 && x.text.length > 10 ) {
                  whisperAudio.markers.push( startSegment );
                  whisperAudio.mtimes.push( 1000 * x.start - 1000 );
                }

                // Word-level timestamps
                x.words.forEach( y => {

                  // Word
                  whisperAudio.words.push( y.word );

                  // Starting time
                  let t = 1000 * y.start;
                  if ( t > 150 ) t -= 150;
                  whisperAudio.wtimes.push( t );

                  // Duration
                  let d = 1000 * (y.end - y.start);
                  if ( d <= 50 ) d = 50;
                  if ( d > 20 ) d -= 20;
                  whisperAudio.wdurations.push( d  );

                });
              }
            });

            d3.select("#playmp3").classed("disabled", false);
          }
        }

      } else {
        d3.select("#jsonmp3").property("value", 'Error: ' + response.status + ' ' + response.statusText );
        console.log(response);
      }

    } catch (error) {
      console.log(error);
    }
  }

}


// RECORDING
let recordingMediaRecorder = null;
let recordingAnalyzer = null;
let recordingIgnoreData = false;
let recordingChunks = [];
const recordingMediaTypes = [
  { type: "audio/webm", ext: "webm" },
  { type: "video/mp4", ext: "mp4" }
];
let recordingMediaType = {};
for( let i=0; i<recordingMediaTypes.length; i++ ) {
  if ( MediaRecorder.isTypeSupported(recordingMediaTypes[i].type) ) {
    recordingMediaType = recordingMediaTypes[i];
    break;
  }
}
const recordingBeep = "data:audio/wav;base64,//uQRAAAAWMSLwUIYAAsYkXgoQwAEaYLWfkWgAI0wWs/ItAAAGDgYtAgAyN+QWaAAihwMWm4G8QQRDiMcCBcH3Cc+CDv/7xA4Tvh9Rz/y8QADBwMWgQAZG/ILNAARQ4GLTcDeIIIhxGOBAuD7hOfBB3/94gcJ3w+o5/5eIAIAAAVwWgQAVQ2ORaIQwEMAJiDg95G4nQL7mQVWI6GwRcfsZAcsKkJvxgxEjzFUgfHoSQ9Qq7KNwqHwuB13MA4a1q/DmBrHgPcmjiGoh//EwC5nGPEmS4RcfkVKOhJf+WOgoxJclFz3kgn//dBA+ya1GhurNn8zb//9NNutNuhz31f////9vt///z+IdAEAAAK4LQIAKobHItEIYCGAExBwe8jcToF9zIKrEdDYIuP2MgOWFSE34wYiR5iqQPj0JIeoVdlG4VD4XA67mAcNa1fhzA1jwHuTRxDUQ//iYBczjHiTJcIuPyKlHQkv/LHQUYkuSi57yQT//uggfZNajQ3Vmz+ Zt//+mm3Wm3Q576v////+32///5/EOgAAADVghQAAAAA//uQZAUAB1WI0PZugAAAAAoQwAAAEk3nRd2qAAAAACiDgAAAAAAABCqEEQRLCgwpBGMlJkIz8jKhGvj4k6jzRnqasNKIeoh5gI7BJaC1A1AoNBjJgbyApVS4IDlZgDU5WUAxEKDNmmALHzZp0Fkz1FMTmGFl1FMEyodIavcCAUHDWrKAIA4aa2oCgILEBupZgHvAhEBcZ6joQBxS76AgccrFlczBvKLC0QI2cBoCFvfTDAo7eoOQInqDPBtvrDEZBNYN5xwNwxQRfw8ZQ5wQVLvO8OYU+mHvFLlDh05Mdg7BT6YrRPpCBznMB2r//xKJjyyOh+cImr2/4doscwD6neZjuZR4AgAABYAAAABy1xcdQtxYBYYZdifkUDgzzXaXn98Z0oi9ILU5mBjFANmRwlVJ3/6jYDAmxaiDG3/6xjQQCCKkRb/6kg/wW+kSJ5//rLobkLSiKmqP/0ikJuDaSaSf/6JiLYLEYnW/+kXg1WRVJL/9EmQ1YZIsv/6Qzwy5qk7/+tEU0nkls3/zIUMPKNX/6yZLf+kFgAfgGyLFAUwY//uQZAUABcd5UiNPVXAAAApAAAAAE0VZQKw9ISAAACgAAAAAVQIygIElVrFkBS+Jhi+EAuu+lKAkYUEIsmEAEoMeDmCETMvfSHTGkF5RWH7kz/ESHWPAq/kcCRhqBtMdokPdM7vil7RG98A2sc7zO6ZvTdM7pmOUAZTnJW+NXxqmd41dqJ6mLTXxrPpnV8avaIf5SvL7pndPvPpndJR9Kuu8fePvuiuhorgWjp7Mf/PRjxcFCPDkW31srioCExivv9lcwKEaHsf/7ow2Fl1T/9RkXgEhYElAoCLFtMArxwivDJJ+bR1HTKJdlEoTELCIqgEwVGSQ+hIm0NbK8WXcTEI0UPoa2NbG4y2K00JEWbZavJXkYaqo9CRHS55FcZTjKEk3NKoCYUnSQ 0rWxrZbFKbKIhOKPZe1cJKzZSaQrIyULHDZmV5K4xySsDRKWOruanGtjLJXFEmwaIbDLX0hIPBUQPVFVkQkDoUNfSoDgQGKPekoxeGzA4DUvnn4bxzcZrtJyipKfPNy5w+9lnXwgqsiyHNeSVpemw4bWb9psYeq//uQZBoABQt4yMVxYAIAAAkQoAAAHvYpL5m6AAgAACXDAAAAD59jblTirQe9upFsmZbpMudy7Lz1X1DYsxOOSWpfPqNX2WqktK0DMvuGwlbNj44TleLPQ+Gsfb+GOWOKJoIrWb3cIMeeON6lz2umTqMXV8Mj30yWPpjoSa9ujK8SyeJP5y5mOW1D6hvLepeveEAEDo0mgCRClOEgANv3B9a6fikgUSu/DmAMATrGx7nng5p5iimPNZsfQLYB2sDLIkzRKZOHGAaUyDcpFBSLG9MCQALgAIgQs2YunOszLSAyQYPVC2YdGGeHD2dTdJk1pAHGAWDjnkcLKFymS3RQZTInzySoBwMG0QueC3gMsCEYxUqlrcxK6k1LQQcsmyYeQPdC2YfuGPASCBkcVMQQqpVJshui1tkXQJQV0OXGAZMXSOEEBRirXbVRQW7ugq7IM7rPWSZyDlM3IuNEkxzCOJ0ny2ThNkyRai1b6ev//3dzNGzNb//4uAvHT5sURcZCFcuKLhOFs8mLAAEAt4UWAAIABAAAAAB4qbHo0tIjVkUU//uQZAwABfSFz3ZqQAAAAAngwAAAE1HjMp2qAAAAACZDgAAAD5UkTE1UgZEUExqYynN1qZvqIOREEFmBcJQkwdxiFtw0qEOkGYfRDifBui9MQg4QAHAqWtAWHoCxu1Yf4VfWLPIM2mHDFsbQEVGwyqQoQcwnfHeIkNt9YnkiaS1oizycqJrx4KOQjahZxWbcZgztj2c49nKmkId44S71j0c8eV9yDK6uPRzx5X18eDvjvQ6yKo9ZSS6l//8elePK/Lf//IInrOF/FvDoADYAGBMGb7 FtErm5MXMlmPAJQVgWta7Zx2go+8xJ0UiCb8LHHdftWyLJE0QIAIsI+UbXu67dZMjmgDGCGl1H+vpF4NSDckSIkk7Vd+sxEhBQMRU8j/12UIRhzSaUdQ+rQU5kGeFxm+hb1oh6pWWmv3uvmReDl0UnvtapVaIzo1jZbf/pD6ElLqSX+rUmOQNpJFa/r+sa4e/pBlAABoAAAAA3CUgShLdGIxsY7AUABPRrgCABdDuQ5GC7DqPQCgbbJUAoRSUj+NIEig0YfyWUho1VBBBA//uQZB4ABZx5zfMakeAAAAmwAAAAF5F3P0w9GtAAACfAAAAAwLhMDmAYWMgVEG1U0FIGCBgXBXAtfMH10000EEEEEECUBYln03TTTdNBDZopopYvrTTdNa325mImNg3TTPV9q3pmY0xoO6bv3r00y+IDGid/9aaaZTGMuj9mpu9Mpio1dXrr5HERTZSmqU36A3CumzN/9Robv/Xx4v9ijkSRSNLQhAWumap82WRSBUqXStV/YcS+XVLnSS+WLDroqArFkMEsAS+eWmrUzrO0oEmE40RlMZ5+ODIkAyKAGUwZ3mVKmcamcJnMW26MRPgUw6j+LkhyHGVGYjSUUKNpuJUQoOIAyDvEyG8S5yfK6dhZc0Tx1KI/gviKL6qvvFs1+bWtaz58uUNnryq6kt5RzOCkPWlVqVX2a/EEBUdU1KrXLf40GoiiFXK///qpoiDXrOgqDR38JB0bw7SoL+ZB9o1RCkQjQ2CBYZKd/+VJxZRRZlqSkKiws0WFxUyCwsKiMy7hUVFhIaCrNQsKkTIsLivwKKigsj8XYlwt/WKi2N4d//uQRCSAAjURNIHpMZBGYiaQPSYyAAABLAAAAAAAACWAAAAApUF/Mg+0aohSIRobBAsMlO//Kk4soosy1JSFRYWaLC4qZBYWFRGZdwqKiwkNBVmoWFSJkWFxX4FFRQWR+LsS4W/rFRb//////////////////////////// /////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////VEFHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAU291bmRib3kuZGUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMjAwNGh0dHA6Ly93d3cuc291bmRib3kuZGUAAAAAAAAAACU=";
const recordingPlaySound = (function beep() {
  const snd = new Audio(recordingBeep);
  return function() { snd.play(); }
})();


async function recordingRecord() {
  if ( !recordingMediaRecorder ) {
    try {
      if ( !navigator.mediaDevices ) throw new Error("No navigator.mediaDevices available for recording.");
      let stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      recordingMediaRecorder = new MediaRecorder(stream, { mimeType: recordingMediaType.type });
      recordingMediaRecorder.ondataavailable = async (event) => {

        // If marked ignore, do not transcribe
        if ( recordingIgnoreData ) {
          recordingIgnoreData = false;
          return;
        } else {
          console.log('Recorded data:',event);
          recordingIgnoreData = false;
        }

        if ( event.data.size > 0 ) {

          // Make a transcription
          const file = new File([event.data], 'file.'+recordingMediaType.ext, { type: recordingMediaType.type });
          const form = new FormData();
          form.append("file", file);

          if ( cfg("whisper-type") === "openai" ) {

            form.append("model", "whisper-1");
            const lang = cfg('theme-lang') || 'en';
            form.append("language", lang);
            const response = await fetch( openaiAudioTranscriptionsProxy , {
              method: "POST",
              body: form,
              headers: {
                "Authorization": "Bearer " + await jwtGet()
              }
            });

            // Append text to input
            if ( response.ok ) {
              let json;
              try {
                json = await response.json();
              } catch(error) {
                console.error("Invalid response from OpenAI");
              }
              if ( json && json.text ) {
                const e = d3.select("#input");
                let text = e.property("value");
                if ( text ) {
                  e.property("value", text + ' ' + json.text );
                } else {
                  e.property("value", json.text );
                }
              }
            }
          } else if ( cfg("whisper-type") === "local" ) {

            form.append("temperature","0.0");
            form.append("temperature_inc", "0.2");
            form.append("response_format", "json");
            const lang = cfg('whisper-language') || 'ru';
            if (lang && lang !== 'auto') form.append("language", lang);

            const response = await fetch( '/api/stt/transcribe' , {
              method: "POST",
              body: form,
              headers: {
                "Authorization": "Bearer " + await jwtGet()
              }
            });

            // Append text to input
            if ( response.ok ) {
              let json;
              try {
                json = await response.json();
              } catch(error) {
                console.error("Invalid response from local Whisper");
              }
              if ( json && json.text ) {
                const e = d3.select("#input");
                let text = e.property("value");

                let s = json.text
                  .replace(/\[[^\]]+\]/g, '')
                  .replaceAll(/[^\p{L}\p{N},\.\p{Quotation_Mark}!€\$\+\p{Dash_Punctuation}%&\?\(\)\* ]/ug,"")
                  .replaceAll("  "," ")
                  .trim();

                if ( text ) {
                  e.property("value", text + ' ' + s );
                } else {
                  e.property("value", s );
                }
                // Прячем индикатор загрузки STT
                const st = d3.select('#stt-status'); if (!st.empty()) st.text('STT: готово');
              }
            } else {
              const st = d3.select('#stt-status'); if (!st.empty()) st.text('STT: ошибка ' + response.status);
            }

          }

        }
      };

      // Connect to TalkingHead
      recordingAnalyzer = head.audioCtx.createAnalyser();
      const recordingSource = head.audioCtx.createMediaStreamSource(stream);
      recordingSource.connect(recordingAnalyzer);

    } catch (error) {
      d3.select("#record").classed("selected",false);
      console.error('Error accessing microphone:', error);
      return;
    }
  }

  try {
    if ( recordingAnalyzer ) {
      head.startListening( recordingAnalyzer,{},(status) => {

        switch(status) {
        case 'start':
          head.lookAtCamera(2000);
          break;
        case 'stop':
          if ( recordingMediaRecorder ) {
            // recordingMediaRecorder.requestData();
            recordingMediaRecorder.stop();
            recordingMediaRecorder.start();
          }
          break;
        case 'maxsilence':
          if ( recordingMediaRecorder ) {
            recordingIgnoreData = true;
            // recordingMediaRecorder.requestData();
            recordingMediaRecorder.stop();
            recordingMediaRecorder.start();
          }
          break;
        case 'maxactive':
          if ( recordingMediaRecorder ) {
            recordingIgnoreData = true;
            // recordingMediaRecorder.requestData();
            recordingMediaRecorder.stop();
            recordingMediaRecorder.start();
          }
          break;
        }
      });
    }

    recordingIgnoreData = false;
    if ( recordingMediaRecorder ) {
      recordingMediaRecorder.start();
    }

  } catch(error) {
    d3.select("#record").classed("selected",false);
    console.error('Recorder error:', error);
  }
}


// Number of words on a string.
function nWords(str) {
  return str ? str.trim().split(/\s+/).length : 0;
}

// Default settings
let CFG = {
  session: 0,
  sessions : [
    {
      name: "Nimetön",
      theme: { lang: 'en', brightness:"dark", ratio:"wide", layout:"port" },
      view: { image: 'NONE' },
      avatar: {},
      camera: { frame: 'full' },
      ai: {},
      voice: { background: "NONE", type: "google", google:{ id: "en-GB-Standard-A"}, lipsync:{ lang: 'en' } }
    },
    {
      name: "Nimetön 2",

      theme: { lang: 'en', brightness: "dark", ratio: "wide", layout: "land" },
      view: { image: 'NONE' },
      avatar: {},
      camera: { frame: 'upper' },
      ai: {},
      voice: { background: "NONE", type: "google", google:{ id: "en-GB-Standard-A"}, lipsync:{ lang: 'en' } }
    },
  ]
};

// Dynamically created icons
const svgSelect = '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M10.5858 13.4142L7.75735 10.5858L6.34314 12L10.5858 16.2427L17.6568 9.1716L16.2426 7.75739L10.5858 13.4142Z" fill="currentColor" /></svg>';
const svgSpeak = '<svg viewBox="-2 -2 28 28" xmlns="http://www.w3.org/2000/svg"><path fill-rule="evenodd" clip-rule="evenodd" d="M12 18.939C14.2091 18.939 16 17.1482 16 14.939C16 12.7299 14.2091 10.939 12 10.939C9.79086 10.939 8 12.7299 8 14.939C8 17.1482 9.79086 18.939 12 18.939ZM12 16.939C13.1046 16.939 14 16.0436 14 14.939C14 13.8345 13.1046 12.939 12 12.939C10.8954 12.939 10 13.8345 10 14.939C10 16.0436 10.8954 16.939 12 16.939Z" fill="currentColor" /><path d="M12 9.04401C13.1046 9.04401 14 8.14858 14 7.04401C14 5.93944 13.1046 5.04401 12 5.04401C10.8954 5.04401 10 5.93944 10 7.04401C10 8.14858 10.8954 9.04401 12 9.04401Z" fill="currentColor" /><path fill-rule="evenodd" clip-rule="evenodd" d="M7 1C5.34315 1 4 2.34315 4 4V20C4 21.6569 5.34315 23 7 23H17C18.6569 23 20 21.6569 20 20V4C20 2.34315 18.6569 1 17 1H7ZM17 3H7C6.44772 3 6 3.44772 6 4V20C6 20.5523 6.44772 21 7 21H17C17.5523 21 18 20.5523 18 20V4C18 3.44772 17.5523 3 17 3Z" fill="currentColor" /></svg>'
const svgStop = '<svg viewBox="-2 -2 28 28" xmlns="http://www.w3.org/2000/svg"><path d="M6.2253 4.81108C5.83477 4.42056 5.20161 4.42056 4.81108 4.81108C4.42056 5.20161 4.42056 5.83477 4.81108 6.2253L10.5858 12L4.81114 17.7747C4.42062 18.1652 4.42062 18.7984 4.81114 19.1889C5.20167 19.5794 5.83483 19.5794 6.22535 19.1889L12 13.4142L17.7747 19.1889C18.1652 19.5794 18.7984 19.5794 19.1889 19.1889C19.5794 18.7984 19.5794 18.1652 19.1889 17.7747L13.4142 12L19.189 6.2253C19.5795 5.83477 19.5795 5.20161 19.189 4.81108C18.7985 4.42056 18.1653 4.42056 17.7748 4.81108L12 10.5858L6.2253 4.81108Z" fill="currentColor" /></svg>';
const svgRepost = '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M13.1459 11.0499L12.9716 9.05752L15.3462 8.84977C14.4471 7.98322 13.2242 7.4503 11.8769 7.4503C9.11547 7.4503 6.87689 9.68888 6.87689 12.4503C6.87689 15.2117 9.11547 17.4503 11.8769 17.4503C13.6977 17.4503 15.2911 16.4771 16.1654 15.0224L18.1682 15.5231C17.0301 17.8487 14.6405 19.4503 11.8769 19.4503C8.0109 19.4503 4.87689 16.3163 4.87689 12.4503C4.87689 8.58431 8.0109 5.4503 11.8769 5.4503C13.8233 5.4503 15.5842 6.24474 16.853 7.52706L16.6078 4.72412L18.6002 4.5498L19.1231 10.527L13.1459 11.0499Z" fill="currentColor" /></svg>';

// Get/set config value for the current session
function cfg(key,value) {
  if ( key === undefined ) return CFG.sessions[CFG.session];
  let parts = key.split('-').map( x => isNaN(x) ? x : parseInt(x) );
  if ( value === undefined ) {
    return parts.reduce( (o,p) => o ? o[p] : undefined, CFG.sessions[CFG.session] );
  } else {
    parts.reduce( (o,p,i) => {
      const def = (typeof parts[i+1] === 'number') ? [] : {};
      return (o[p] = (parts.length-1) === i ? value : (o[p] || def));
    }, CFG.sessions[CFG.session] );
  }
}

// Load settings
try {
  let json = localStorage.getItem('talkinghead');
  CFG = (json ? JSON.parse(json) : CFG);
} catch(error) {
  alert("Invalid JSON settings");
  console.error(error);
}

window.cfg = cfg;

// Значения из вкладки «Распознавание и синтез речи» (speech_config.js).
// Панель аватара переопределяет их только если пользователь выбрал голос там.
// Без этого аватар говорил жёстко 'xenia', и смена голоса в настройках на него
// не влияла вовсе.
function speechPref(name, dflt) {
  try {
    const sc = window.SpeechConfig;
    const f = sc && sc[name];
    return (typeof f === 'function') ? f.call(sc) : dflt;
  } catch (e) {
    return dflt;
  }
}
function avatarVoice() {
  return cfg('voice-silero-speaker') || speechPref('ttsSpeaker', 'xenia') || 'xenia';
}
function avatarRate() {
  return cfg('voice-silero-rate') || speechPref('ttsRate', '1.0') || '1.0';
}
function avatarPitch() {
  return cfg('voice-silero-pitch') || speechPref('ttsPitch', '0') || '0';
}

// === 3D-аватар ==========================================================
// По умолчанию выключен. Движок (three.js + VRM) тяжёлый: без этого флага
// модель грузилась при каждом открытии /desktop и заметно тормозила страницу.
// Переключатель — в панели «Avatar» (desktop.html#avatar3dSwitch).
const AVATAR3D_KEY = 'avatar-3d';

function avatar3dEnabled() {
  try { return cfg(AVATAR3D_KEY) === true; } catch (e) { return false; }
}

// Показывает/скрывает блок настроек модели и сам холст.
function syncAvatar3dUi() {
  const on = avatar3dEnabled();
  const state = document.getElementById('avatar3dState');
  const sw    = document.getElementById('avatar3dSwitch');
  const opts  = document.getElementById('avatar3dOptions');
  if (state) state.textContent = on ? 'Включён' : 'Выключен';
  if (sw)    sw.classList.toggle('selected', on);
  // Выключенные настройки держим видимыми, но неактивными — так понятнее,
  // что они появятся, если включить аватар.
  if (opts) {
    opts.style.opacity = on ? '' : '.45';
    opts.style.pointerEvents = on ? '' : 'none';
  }
  try { d3.select('#avatar').style('display', on ? '' : 'none'); } catch (e) {}
}

async function setAvatar3D(on) {
  try { cfg(AVATAR3D_KEY, !!on); saveConfig(); } catch (e) {}
  syncAvatar3dUi();
  if (on) {
    // Модель грузим только теперь — до этого движок не запускали вовсе.
    const selected = d3.select("[data-item='avatar-name'].selected");
    if (selected.empty()) {
      d3.select("[data-item='avatar-name']").classed('selected', true).dispatch('click');
    } else {
      // После выключения canvas очищен, но head.avatar всё ещё помнит URL,
      // и обработчик клика пропускает showAvatar как «та же модель» — аватар
      // оставался пустым. Сбрасываем кэш, чтобы модель загрузилась заново.
      try { if (typeof head !== 'undefined' && head) head.avatar = null; } catch (e) {}
      selected.dispatch('click');
    }
  } else {
    try { if (typeof head !== 'undefined' && head && typeof head.stop === 'function') head.stop(); }
    catch (e) { /* движок мог не стартовать — тогда и чистить нечего */ }
    try { d3.select('#avatar').html(''); } catch (e) {}
  }
}

// === Живое применение настроек аватара ===================================
// CFG читается из localStorage только при загрузке страницы. Изменения в
// другом окне (панель на /desktop и оверлей /avatar) раньше не доходили до
// уже созданного TalkingHead — модель/фильтры обновлялись лишь после
// перезагрузки. Эти функции применяют настройки сразу и сохраняют их.

function applyAvatarFilter() {
  try {
    const filters = [];
    d3.selectAll("[data-avatar-brightness],[data-avatar-contrast],[data-avatar-saturate]").nodes().forEach(node => {
      const key = node.dataset.avatarBrightness ? 'brightness'
        : node.dataset.avatarContrast ? 'contrast'
        : node.dataset.avatarSaturate ? 'saturate' : '';
      if (key) filters.push(key + '(' + node.value + ')');
    });
    d3.select('#avatar').style('filter', filters.length ? filters.join(' ') : null);
  } catch (e) { /* страница без панели аватара */ }
}

function syncAvatarUiFromConfig() {
  try {
    const name = cfg('avatar-name');
    if (name) {
      d3.selectAll('[data-avatar-name]').classed('selected', false);
      d3.selectAll("[data-avatar-name='" + name + "']").classed('selected', true);
    }
    const body = cfg('avatar-body');
    if (body) {
      d3.selectAll('[data-avatar-body]').classed('selected', false);
      d3.selectAll("[data-avatar-body='" + body + "']").classed('selected', true);
    }
    const mood = cfg('avatar-mood');
    if (mood) {
      d3.selectAll('[data-mood]').classed('selected', false);
      d3.selectAll("[data-mood='" + mood + "']").classed('selected', true);
    }
    const frame = cfg('camera-frame');
    if (frame) {
      d3.selectAll('[data-camera-frame]').classed('selected', false);
      d3.selectAll("[data-camera-frame='" + frame + "']").classed('selected', true);
    }
    ['avatar-brightness', 'avatar-contrast', 'avatar-saturate'].forEach(key => {
      const value = cfg(key);
      if (value !== undefined) {
        d3.selectAll("[data-item='" + key + "']").property('value', value);
      }
    });
    const urlEl = d3.select("[data-item='avatar-url']");
    if (urlEl.node() && cfg('avatar-url') !== undefined) urlEl.property('value', cfg('avatar-url'));
  } catch (e) { /* панель могла быть не отрисована */ }
  applyAvatarFilter();
}

/* Камера и настроение хранятся в конфиге, но раньше применялись только
   кликом по панели — окно-оверлей мини-приложения их не перечитывало. */
function applyAvatarView() {
  if (!head || !head.setView) return;
  const frame = cfg('camera-frame');
  if (!frame) return;
  try {
    head.setView(frame, {
      cameraX: parseFloat(cfg('camera-x') || 0),
      cameraY: parseFloat(cfg('camera-y') || 0),
      cameraDistance: parseFloat(cfg('camera-d') || 0),
      cameraRotateX: parseFloat(cfg('camera-rotx') || 0),
      cameraRotateY: parseFloat(cfg('camera-roty') || 0)
    });
  } catch (e) { /* модель ещё не готова — применится после загрузки */ }
}

function applyAvatarMood() {
  if (!head || !head.setMood) return;
  const mood = cfg('avatar-mood');
  if (!mood) return;
  try { head.setMood(mood); } catch (e) { /* у модели нет мимики */ }
}

async function applyAvatarSettings(opts = {}) {
  applyAvatarFilter();
  if (!head) return;
  /* В окне-оверлее модель показывается всегда, даже когда 3D-аватар в
     настройках выключен: иначе смена модели из другого окна до него не
     доходила. На /desktop без флага движок не трогаем. */
  const embed = /[?&]embed=1/.test(location.search);
  if (!avatar3dEnabled() && !embed) return;
  const name = cfg('avatar-name');
  if (!name || name === 'FILE') return;
  let target = null;
  if (name === 'URL') {
    const url = cfg('avatar-url');
    if (!url) return;
    target = { url: url, body: cfg('avatar-body') };
  } else if (site.avatars && site.avatars[name]) {
    target = Object.assign({}, site.avatars[name]);
    if (cfg('avatar-body')) target.body = cfg('avatar-body');
  }
  if (!target) return;
  const current = head.avatar || {};
  const changed = !!opts.force ||
    (target.url && target.url !== current.url) ||
    (target.body && target.body !== current.body);
  if (!changed || head.__infinityApplying) return;
  head.__infinityApplying = true;
  try {
    await head.showAvatar(target, progressUpdate);
    try { headLoaded(); } catch (e) {}
    applyAvatarView();
    applyAvatarMood();
  } catch (e) {
    console.warn('[Infinity] не удалось применить настройки аватара:', e);
  } finally {
    head.__infinityApplying = false;
  }
}

function reloadAvatarConfigFromStorage() {
  try {
    const raw = localStorage.getItem('talkinghead');
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return;
    CFG = parsed;
    /* Без сессии применять нечего, а пустой массив сломал бы saveConfig:
       он пишет cfg() и получил бы undefined. */
    if (!Array.isArray(CFG.sessions) || !CFG.sessions.length) return;
    if (typeof CFG.session !== 'number') CFG.session = 0;
    syncAvatarUiFromConfig();
    applyAvatarSettings();
    applyAvatarView();
    applyAvatarMood();
  } catch (e) { /* повреждённый JSON не должен ломать страницу */ }
}

window.addEventListener('storage', function (e) {
  if (e.key === 'talkinghead' && e.newValue) reloadAvatarConfigFromStorage();
});
// Другие страницы (рабочий стол при смене профиля) могут попросить
// перечитать настройки аватара из localStorage без перезагрузки.
try { window.__infinityReloadAvatar = reloadAvatarConfigFromStorage; } catch (e) {}
try {
  const _avatarBC = new BroadcastChannel('infinity-avatar');
  _avatarBC.onmessage = function (e) {
    if (e.data && e.data.type === 'config-changed') reloadAvatarConfigFromStorage();
  };
  window.__infinityAvatarBC = _avatarBC;
} catch (e) { /* BroadcastChannel может быть недоступен */ }

// Save config to localStorage and show active session config
let loadingConfig = false;
function saveConfig() {
  if ( !loadingConfig ) {
    let json = JSON.stringify(CFG);
    localStorage.setItem("talkinghead", json);
    json = JSON.stringify( cfg() );
    const jsonEl = d3.select("#json");
    if ( !jsonEl.empty() ) jsonEl.property("value", json );
    // Другие окна (оверлей /avatar, панель на /desktop) узнают об изменении
    // сразу. storage-событие не приходит в то же окно, поэтому дублируем
    // через BroadcastChannel — тогда все открытые вкладки применяют настройки
    // без перезапуска сервера и перезагрузки страницы.
    try {
      if (window.__infinityAvatarBC) {
        window.__infinityAvatarBC.postMessage({ type: 'config-changed' });
      }
    } catch (e) { /* канал мог быть закрыт */ }
  }
}

// Load config for the given session
function loadConfig(session = null) {
  try {
    loadingConfig = true;
    let json = localStorage.getItem('talkinghead');
    const config = (json ? JSON.parse(json) : CFG);
    if ( config && config.hasOwnProperty("session") && config.hasOwnProperty("sessions") && config.sessions.length ) {
      CFG = config;
      if ( session !== null && session >= 0 && session < CFG.sessions.length ) {
        CFG.session = session;
        json = JSON.stringify(CFG);
        localStorage.setItem("talkinghead", json);
      }

      // Populate sessions
      d3.selectAll(".session").nodes().forEach( n => {
        const e = d3.select(n);
        const id = parseInt( e.property("id").split("-")[1] );
        if ( id >= 0 && id < CFG.sessions.length ) {
          e.classed("selected", (id === CFG.session) );
        } else {
          e.remove();
        }
      });
      for( let i=0; i<CFG.sessions.length; i++ ) {
        const e = d3.select("#session-"+i);
        if ( e.empty() ) {
          d3.select("#sessions").append("div")
            .property("id","session-"+i)
            .classed("session", true)
            .classed("selected", (i === CFG.session) );
        }
      }

      // Populate directory
      d3.selectAll(".entry").nodes().forEach( n => {
        const e = d3.select(n);
        const id = parseInt( e.property("id").split("-")[1] );
        if ( id >= 0 && id < CFG.sessions.length ) {
          let name = CFG.sessions[id].name;
          if ( !name || name.length === 0 ) {
            name = "Nimetön";
            CFG.sessions[id].name = name;
          }
          e.select("div")
            .classed("selected", (id === CFG.session) )
            .text( name );
        } else {
          e.remove();
        }
      });
      for( let i=0; i<CFG.sessions.length; i++ ) {
        const e = d3.select("#entry-"+i);
        if ( e.empty() ) {
          let n = d3.select("#directory").node().lastElementChild;
          let name = CFG.sessions[i].name;
          if ( !name || name.length === 0 ) {
            name = "Nimetön";
            CFG.sessions[i].name = name;
          }
          let clone = d3.select(n).clone(true);
          clone.property("id","entry-"+i);
          clone.select("[data-session]")
            .attr("data-session", i)
            .classed("selected", (i === CFG.session) )
            .text(name)
            .on('click.command', entrySelect);
          clone.selectAll("[data-entry-move]")
            .on('click.command', entryMove );
        }
      }

      // Populate settings page in specific order
      [
        "[data-item='view-url']", "[data-item='avatar-url']",
        "[data-item='avatar-body']","[data-item]"
      ].forEach( x => {
        d3.selectAll(x).nodes().forEach( (n) => {
          const e = d3.select(n);
          const item = e.attr("data-item");
          const type = e.attr("data-type");
          const range = e.attr("data-range");
          let value = cfg( item );
          if ( value !== undefined ) {
            if ( type === 'boolean' ) {
              e.classed( "selected", value )
            } else if ( type === 'option' ) {
              if ( value === e.attr("data-"+item) ) {
                e.dispatch("click");
              }
            } else if ( type === 'value' ) {
              e.property("value",value).dispatch("change");
            }
          } else {
            if ( type === 'boolean' ) {
              cfg( item, e.classed( "selected" ) );
            } else if ( type === 'option' ) {
              if ( e.classed("selected") ) {
                cfg( item, e.attr("data-"+item) );
                e.dispatch("click");
              }
            } else {
              if ( range !== null ) {
                cfg( item, parseFloat( e.property("value") ) );
              } else {
                cfg( item, e.property("value") );
              }
              e.dispatch("change");
            }
          }
        });
      });

      // Populate other parts of UI,
      d3.select("#name").text( cfg("name") );
      if ( d3.select("[data-item='view-image'].selected").empty() ) {
        d3.select("[data-item='view-image']").classed("selected",true).dispatch('click');
      }
      // 3D-аватар по умолчанию НЕ грузим: движок (three.js + VRM) тяжёлый и
      // стартовал при каждом открытии страницы. Включается вручную переключателем
      // в панели «Avatar» (setAvatar3D).
      syncAvatar3dUi();
      if ( avatar3dEnabled() ) {
        if ( d3.select("[data-item='avatar-name'].selected").empty() ) {
          d3.select("[data-item='avatar-name']").classed("selected",true).dispatch('click');
        }
      }
      scriptInit();
      json = JSON.stringify( cfg() );
      const jsonEl = d3.select("#json");
      if ( !jsonEl.empty() ) jsonEl.property("value",json);
    }
  } catch(error) {
    alert("Invalid JSON settings");
    console.error(error);
  } finally {
    loadingConfig = false;
    saveConfig();
  }
}

// Process string s for parts to exclude from speech/lip-sync
// For continued stream of strings the previous state o can be given
function excludesProcess(s,o=null) {

  // If no previous rules and states, build rules based on user settings
  if ( !o || !o.rules || !Array.isArray(o.rules) ) {
    o = { rules: [] };
    if ( cfg('voice-exclude-italics') ) {
      o.rules.push( { separator: '*', open: false });
    }
    if ( cfg('voice-exclude-code') ) {
      o.rules.push( { separator: '```', open: false });
    }
  }

  // Excludes is an array of [start,end] index pairs
  o.excludes = [];

  // If there are rules, process them
  o.rules.forEach( x => {
    const parts = s.split(x.separator);
    let i = 0;
    parts.forEach( (y,j) => {
      const isLast = (j === (parts.length - 1));
      if ( x.open ) {
        const start = i - (j===0 ? 0 : x.separator.length);
        const end = i + y.length - 1 + (isLast ? x.separator.length : 0);
        o.excludes.push( [start, end] ); // Exclude
      }
      if ( !isLast ) {
        i += y.length + x.separator.length;
        x.open = !x.open;
      }
    });
  });

  return o;
}

function motion( action, pose, gesture, mood ) {
  if (!head) return;
  try {
    head.setMood(mood || 'neutral');
  } catch(err) {}
  if ( gesture && site.gestures[gesture] ) {
    head.playGesture(site.gestures[gesture].name);
  }
  if ( action && site.animations[action] ) {
    head.playAnimation( site.animations[action].url, progressUpdate, site.animations[action].dur || 20 );
  } else if ( pose && site.poses[pose] ) {
    head.playPose( site.poses[pose].url, progressUpdate, site.poses[pose].dur || 60 );
  }
}

// Build outgoing message for OpenAI
function openaiBuildMessage() {
  const msgs = [];

  const systems = [
    { sel: "[data-ai-openai-system]", role: "system" },
    { sel: "[data-ai-openai-user1]", role: "user" },
    { sel: "[data-ai-openai-ai1]", role: "assistant" },
    { sel: "[data-ai-openai-user2]", role: "user" },
    { sel: "[data-ai-openai-ai2]", role: "assistant" }
  ];
  const session = d3.select("#right-sessions .session.selected");
  const input = d3.select("#input");
  const messages = session.selectAll(".message:not(.grayed)");

  const limitDialog = parseInt(cfg('ai-openai-dialog')) || 4;
  const limitInput = parseInt(cfg('ai-openai-input')) || 2000;
  // Новый лимит: сколько последних пар user/assistant брать в контекст.
  // По умолчанию 6 — компромисс между памятью и шумом.
  const ctxPairs = parseInt(cfg('ai-context-pairs')) || 6;
  let dialogs = 0;
  let pairCount = 0;
  let words = 0;

  // System messages
  systems.forEach( x => {
    const n = d3.select(x.sel).node();
    if ( n.value && n.value.length ) {
      if ( n.dataset.words ) {
        words += parseInt( n.dataset.words );
      } else {
        let wc = nWords( n.value );
        n.dataset.words = wc;
        words += wc;
      }
      msgs.push( { n: n, role: x.role, content: n.value } );
    }
  });

  // messages in reverse order — пара = один user + один assistant
  const revmsgs = [];
  revmsgs.push( { n: input.node(), role: "user", content: input.property("value") } );
  words += nWords( input.property("value") );
  // текущее сообщение считается как «1 пара сверху лимита» — поэтому ждём пары из истории
  const reversed = messages.nodes().reverse();
  for (const n of reversed) {
    if ( dialogs >= limitDialog || words >= limitInput || pairCount >= ctxPairs * 2 ) break;
    let role;
    let val;
    if ( n.dataset.input && n.dataset.input.length ) {
      role = "user";
      val = n.dataset.input;
    } else if ( n.dataset.output && n.dataset.output.length ) {
      role = "assistant";
      val = n.dataset.output;
    }
    if ( role && val ) {
      revmsgs.push( { n: n, role: role, content: val } );
      if ( n.dataset.words ) {
        words += parseInt( n.dataset.words );
      } else {
        let wc = nWords( val );
        n.dataset.words = wc;
        words += wc;
      }
      if ( role === "user" ) pairCount++;
      dialogs++;
    }
  }

  // Build message
  msgs.push( ...revmsgs.reverse() );

  return msgs;
}

// Moderate messages
async function openaiModerateMessage(msgs) {
  // Known status
  let flag = msgs.reduce( (a,b) => ((b.n && b.n.dataset.flag !== undefined) ? ( a | (b.n.dataset.flag === 'true') ) : a ), false);
  let score = msgs.reduce( (a,b) => ((b.n && b.n.dataset.score !== undefined) ? Math.max( a, parseFloat(b.n.dataset.score) ) : a ), 0.0);

  // Moderate only yet unmoderated messages, add the grouped message
  const modMsgs = msgs.filter( x => ( x.n && (x.n.dataset.flag === undefined) && x.content && x.content.length ) );
  if ( modMsgs.length === 0 ) return false;
  if ( msgs.length > 1 ) {
    const full = msgs.map( x => x.content ).join('\n\n');
    modMsgs.push( { content: full } );
  }

  // Endpoint/proxy and authentication
  let url;
  let headers = { "Content-Type": "application/json" };
  const apikey = d3.select("#apikey-openai").property("value");
  if ( apikey ) {
    url = openaiModerationsEndpoint;
    headers["Authorization"] = "Bearer " + apikey;
  } else {
    url = openaiModerationsProxy;
    headers["Authorization"] = "Bearer " + await jwtGet()
  }

  const text = modMsgs.map( x => x.content );
  try {
    const res = await fetch( url, {
      method: "POST",
      mode: "cors",
      headers: headers,
      body: JSON.stringify({
        input: text
      })
    });

    if ( res.ok ) {

      const data = await res.json();

      if ( data && data.results && data.results.length ) {
        data.results.forEach( (x,i) => {
          let flagtmp = Boolean( x.flagged );
          let scoretmp = 0.0;
          Object.keys(x.categories).forEach( key => {
            scoretmp = Math.max( scoretmp, x.category_scores[key] );
          });

          if ( modMsgs[i].n ) {
            modMsgs[i].n.dataset.flag = flagtmp;
            modMsgs[i].n.dataset.score = scoretmp;
          }

          flag |= flagtmp;
          score = Math.max(score,scoretmp);
        });
      }

      d3.select("#flag").classed("selected",flag);
      d3.select("#score").text( "" + Math.round(100 * score) / 100 );
    } else {
      console.error("Error: Moderation API returned ", res.status);
    }

  } catch (error) {
    console.error("Error: Moderation API returned ", error);
  }

  return flag;
}

// Send messages to OpenAI API and handle streamed response
async function openaiSendMessage(node,msgs) {

  // Create a new AbortController instance
  aiController = new AbortController();
  const signal = aiController.signal;

  // Endpoint/proxy and authentication
  let url;
  let headers = { "Content-Type": "application/json" };
  const apikey = d3.select("#apikey-openai").property("value");
  if ( apikey ) {
    url = openaiChatCompletionsEndpoint;
    headers["Authorization"] = "Bearer " + apikey;
  } else {
    url = openaiChatCompletionsProxy;
    headers["Authorization"] = "Bearer " + await jwtGet()
  }

  // Chat completion
  try {
    // Message body
    const sysMsgs = msgs.filter(m => m.role === 'system');
    console.log('[openai] sending', msgs.length, 'msgs; system=', sysMsgs.length, 'sys0.len=', sysMsgs[0] ? sysMsgs[0].content.length : 0, 'cfg-sys=', (cfg('ai-openai-system')||'').length, 'DOM-sys=', (d3.select('[data-ai-openai-system]').node()?.value || '').length);
    const body = {
      model: cfg('ai-model'),
      messages: msgs.map( x => {
        const {role,content,name} = x;
        return (name ? {role,content,name} : {role,content});
      }),
      temperature: cfg('ai-openai-temperature'),
      presence_penalty: cfg('ai-openai-presence'),
      frequency_penalty: cfg('ai-openai-frequency'),
      max_tokens: cfg('ai-openai-output'),
      stream: true
    };
    const stop = cfg('ai-openai-stop');
    if ( stop && stop.length ) {
      body.stop = [ stop ];
    }
    const user = cfg('ai-openai-user');
    if ( user && user.length ) {
      body.user = user;
    }
    const isMotionSelected = d3.select("#motion").classed("selected");
    if ( isMotionSelected ) {
      body.tools = [
        {
          "type": "function",
          "function": {
            "name": "move_body",
            "description": "Set the action, still pose and/or mood of your avatar's body in virtual world",
            "parameters": {
              "type": "object",
              "properties": {
                "action": {
                  "type": "string",
                  "enum": Object.keys(site.animations)
                },
                "stillpose": {
                  "type": "string",
                  "enum": Object.keys(site.poses)
                },
                "gesture": {
                  "type": "string",
                  "enum": Object.keys(site.gestures)
                },
                "mood": {
                  "type": "string",
                  "enum": Object.keys(head.animMoods)
                }
              },
              "required": []
            }
          }
        }
      ];
      body.tool_choice = "auto";
      body.parallel_tool_calls = false;
    }

    // Elements
    node.dataset.output = '';
    let tts;
    let fn;

    do {

      // Ready for either text response or a function call
      tts = '';
      fn = { id: '', name: '', arguments: '' };

      // Fetch the response from the OpenAI API
      const res = await fetch( url, {
        method: "POST",
        mode: "cors",
        headers: headers,
        body: JSON.stringify(body),
        signal
      });

      if ( res.ok ) {

        // Read the response as a stream of data
        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let exclude = null; // Exclude object
        let sseBuffer = '';

        while (true) {
          let { done, value } = await reader.read();

          // Extract text data
          if ( value ) {
            // stream:true важен для много­байтных UTF-8 на границах чанков
            sseBuffer += decoder.decode(value, { stream: true });
            const parts = sseBuffer.split('\n');
            // Последний элемент может быть неполным — оставляем в буфере
            sseBuffer = parts.pop() || '';
            for (const data of parts) {
              if ( !data.startsWith("data: ") ) continue;
              const line = data.substring(6).trim();
              if ( !line || line === '[DONE]' ) continue;
              let obj;
              try {
                obj = JSON.parse( line );
              } catch(parseError) {
                // Мягкий парсинг — пропускаем битую строку, не прерывая стрим
                console.warn('[openaiSend] skip bad JSON line:', line.substring(0, 80));
                continue;
              }
              if ( obj && obj.error ) throw new Error( obj.error.message || ''+res.status );
              const delta = obj?.choices?.[0]?.delta;
              if (delta && delta.tool_calls && delta.tool_calls[0]) {
                fn.id += delta.tool_calls[0].id || '';
                fn.name += delta.tool_calls[0].function?.name || '';
                fn.arguments += delta.tool_calls[0].function?.arguments || '';
              }
              const piece = delta?.content || '';
              if ( piece ) {
                node.dataset.output += piece;
                tts += piece;
                addText(node, piece);   // прямой апдейт DOM во время стрима
              }
            }
          }

          // Поточная озвучка (пока не done)
          if ( tts ) {
            let idx = 0;
            ['.','!','?','\n'].forEach( x => {
              let tmp = tts.lastIndexOf(x);
              if ( tmp > idx ) idx = tmp + 1;
            });
            if ( idx > 10 ) {
              let s = tts.substring(0, idx);
              if ( s && s.length ) {
                tts = tts.substring(idx).trimStart();
                if ( cfg('voice-type') === 'eleven' ) {
                  await elevenSpeak( s + " ", node );
                } else if ( cfg('voice-type') === 'microsoft' ) {
                  await microsoftSpeak( s + " ", node );
                } else if ( cfg('voice-type') === 'silero' ) {
                  console.log('[Chat] Silero accumulate:', s);
                  await sileroSpeakViaHead(s, node);
                } else {
                  exclude = excludesProcess(s,exclude);
                  await head.speakText( s, {
                    lipsyncLang: cfg('voice-lipsync-lang'),
                    ttsVoice: cfg('voice-google-id'),
                    ttsLang: cfg('voice-google-id').substring(0,5),
                    ttsRate: cfg('voice-google-rate'),
                    ttsPitch: cfg('voice-google-pitch')
                  }, addText.bind(null,node), exclude.excludes );
                }
              }
            }
          }

          if (done) break;
        }

        if ( fn.id ) {
          // Function call
          body.messages.push({
            role: "assistant",
            content: null,
            tool_calls: [
              {
                id: fn.id,
                type: "function",
                function: { name: fn.name, arguments: fn.arguments }
              }
            ]
          });
          try {
            const m = JSON.parse(fn.arguments);
            motion(m.action, m.stillpose, m.gesture, m.mood);
          } catch(motionError) {
            console.error(motionError);
          }
          body.messages.push({ role: "tool", content: "", tool_call_id: fn.id });
          body.tool_choice = "none";
        }

        // Финальная озвучка остатка
        if ( tts ) {
          if ( cfg('voice-type') === 'eleven' ) {
            await elevenSpeak( tts + " ", node );
          } else if ( cfg('voice-type') === 'microsoft' ) {
            await microsoftSpeak( tts + " ", node );
          } else if ( cfg('voice-type') === 'silero' ) {
            if (sileroAccumulatedText.trim())
              await sileroSpeakViaHead(sileroAccumulatedText.trim(), sileroNode || node);
          } else {
            exclude = excludesProcess(tts,exclude);
            await head.speakText( tts, {
              lipsyncLang: cfg('voice-lipsync-lang'),
              ttsVoice: cfg('voice-google-id'),
              ttsLang: cfg('voice-google-id').substring(0,5),
              ttsRate: cfg('voice-google-rate'),
              ttsPitch: cfg('voice-google-pitch')
            }, addText.bind(null,node), exclude.excludes );
          }
        }

        // Moderate
        let flag = await openaiModerateMessage([{ content: node.dataset.output, n: node }]);

      }
    else {
        console.error( await res.text() );
        throw new Error( json.error ? json.error.message : ''+res.status );
      }

    } while( fn.id ); // Repeat, if this iteration was a function call

  } catch (error) {
    if ( signal.aborted ) error = "aborted";
    console.error(error);
    addText( node, ' [' + error + ']');
  } finally {
    aiController = null; // Reset the AbortController instance
    if ( elevenSocket ) {
      elevenSpeak("",null);
    }
    microsoftSpeak(null);
    sileroReset();

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });
  }
}

// Grok-beta

// Send messages to local LLama API and handle streamed response
async function grokSendMessage(node,msgs) {

  // Create a new AbortController instance
  aiController = new AbortController();
  const signal = aiController.signal;

  // Endpoint/proxy and authentication
  let url;
  let headers = { "Content-Type": "application/json" };
  const apikey = d3.select("#apikey-grok").property("value");
  if ( apikey ) {
    url = grokChatCompletionsEndpoint;
    headers["Authorization"] = "Bearer " + apikey;
  } else {
    url = grokChatCompletionsProxy;
    headers["Authorization"] = "Bearer " + await jwtGet()
  }

  // Chat completion
  try {
    // Message body
    const outMessages = msgs.map( x => {
      const {role,content,name} = x;
      return (name ? {role,content,name} : {role,content});
    });
    const sysMsgs = outMessages.filter(m => m.role === 'system');
    console.log('[grok] sending', outMessages.length, 'msgs; system=', sysMsgs.length, 'sys0.len=', sysMsgs[0] ? sysMsgs[0].content.length : 0, 'cfg-sys=', (cfg('ai-openai-system')||'').length, 'DOM-sys=', (d3.select('[data-ai-openai-system]').node()?.value || '').length);
    const body = {
      model: cfg('ai-model'),
      messages: outMessages,
      temperature: cfg('ai-openai-temperature'),
      presence_penalty: cfg('ai-openai-presence'),
      frequency_penalty: cfg('ai-openai-frequency'),
      max_tokens: cfg('ai-openai-output'),
      stream: true
    };
    const stop = cfg('ai-grok-stop');
    if ( stop && stop.length ) {
      body.stop = [ stop ];
    }
    const user = cfg('ai-grok-user');
    if ( user && user.length ) {
      body.user = user;
    }

    // Elements
    node.dataset.output = '';
    let tts = '';

    // Fetch the response from the Llama.cpp server
    tts = '';
    const res = await fetch( url, {
      method: "POST",
      mode: "cors",
      headers: headers,
      body: JSON.stringify(body),
      signal
    });

    if ( res.ok ) {

      // Read the response as a stream of data
      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let exclude = null; // Exclude object
      let sseBuffer = '';

      while (true) {
        let { done, value } = await reader.read();

        // Extract text data
        if ( value ) {
          sseBuffer += decoder.decode(value, { stream: true });
          const parts = sseBuffer.split('\n');
          sseBuffer = parts.pop() || '';
          for (const data of parts) {
            if ( !data.startsWith("data: ") ) continue;
            const line = data.substring(6).trim();
            if ( !line || line === '[DONE]' ) continue;
            let obj;
            try {
              obj = JSON.parse( line );
            } catch(parseError) {
              console.warn('[grokSend] skip bad JSON line:', line.substring(0, 80));
              continue;
            }
            if ( obj && obj.error ) throw new Error( obj.error.message || ''+res.status );
            const piece = obj?.choices?.[0]?.delta?.content || '';
            if ( piece ) {
              node.dataset.output += piece;
              tts += piece;
              addText(node, piece);
            }
          }
        }

        // Поточная озвучка во время стрима
        if ( tts ) {
          let idx = 0;
          ['.','!','?','\n'].forEach( x => {
            let tmp = tts.lastIndexOf(x);
            if ( tmp > idx ) idx = tmp + 1;
          });
          if ( idx > 10 ) {
            let s = tts.substring(0, idx);
            if ( s && s.length ) {
              tts = tts.substring(idx).trimStart();
              if ( cfg('voice-type') === 'eleven' ) {
                await elevenSpeak( s + " ", node );
              } else if ( cfg('voice-type') === 'microsoft' ) {
                await microsoftSpeak( s + " ", node );
              } else if ( cfg('voice-type') === 'silero' ) {
                await sileroSpeakViaHead(s, node);
                sileroNode = node;
              } else {
                exclude = excludesProcess(s, exclude);
                await head.speakText( s, {
                  lipsyncLang: cfg('voice-lipsync-lang'),
                  ttsVoice: cfg('voice-google-id'),
                  ttsLang: cfg('voice-google-id').substring(0,5),
                  ttsRate: cfg('voice-google-rate'),
                  ttsPitch: cfg('voice-google-pitch')
                }, addText.bind(null,node), exclude.excludes );
              }
            }
          }
        }

        if (done) {
          // Финальная озвучка остатка
          if ( tts ) {
            if ( cfg('voice-type') === 'eleven' ) {
              await elevenSpeak( tts + " ", node );
            } else if ( cfg('voice-type') === 'microsoft' ) {
              await microsoftSpeak( tts + " ", node );
            } else if ( cfg('voice-type') === 'silero' ) {
              if (sileroAccumulatedText.trim())
                await sileroSpeak(sileroAccumulatedText.trim(), sileroNode || node);
            } else {
              exclude = excludesProcess(tts,exclude);
              await head.speakText( tts, {
                lipsyncLang: cfg('voice-lipsync-lang'),
                ttsVoice: cfg('voice-google-id'),
                ttsLang: cfg('voice-google-id').substring(0,5),
                ttsRate: cfg('voice-google-rate'),
                ttsPitch: cfg('voice-google-pitch')
              }, addText.bind(null,node), exclude.excludes );
            }
          }
          break;
        }
      }

    }
    else {
        console.error( await res.text() );
        throw new Error( json.error ? json.error.message : ''+res.status );
      }

  } catch (error) {
    if ( signal.aborted ) error = "aborted";
    console.error(error);
    addText( node, ' [' + error + ']');
  } finally {
    aiController = null; // Reset the AbortController instance
    if ( elevenSocket ) {
      elevenSpeak("",null);
    }
    microsoftSpeak(null);
    sileroReset();

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });
  }
}

// Llama

// Send messages to local LLama API and handle streamed response
async function llamaSendMessage(node,msgs) {

  // Create a new AbortController instance
  aiController = new AbortController();
  const signal = aiController.signal;

  // Chat completion
  try {
    // Message body
    const sysMsgs = msgs.filter(m => m.role === 'system');
    console.log('[llama] sending', msgs.length, 'msgs; system=', sysMsgs.length, 'sys0.len=', sysMsgs[0] ? sysMsgs[0].content.length : 0, 'cfg-sys=', (cfg('ai-openai-system')||'').length, 'DOM-sys=', (d3.select('[data-ai-openai-system]').node()?.value || '').length);
    const body = {
      model: cfg('ai-model'),
      messages: msgs.map( x => {
        const {role,content,name} = x;
        return (name ? {role,content,name} : {role,content});
      }),
      temperature: cfg('ai-openai-temperature'),
      presence_penalty: cfg('ai-openai-presence'),
      frequency_penalty: cfg('ai-openai-frequency'),
      max_tokens: cfg('ai-openai-output'),
      stream: true
    };
    const stop = cfg('ai-llama-stop');
    if ( stop && stop.length ) {
      body.stop = [ stop ];
    }
    const user = cfg('ai-llama-user');
    if ( user && user.length ) {
      body.user = user;
    }

    // Elements
    node.dataset.output = '';
    let tts = '';

    // Fetch the response from the Llama.cpp server
    tts = '';
    const res = await fetch( llamaChatCompletionsProxy, {
      method: "POST",
      mode: "cors",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + await jwtGet()
      },
      body: JSON.stringify(body),
      signal
    });

    if ( res.ok ) {

      // Read the response as a stream of data
      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let exclude = null; // Exclude object
      let sseBuffer = '';

      while (true) {
        let { done, value } = await reader.read();

        // Extract text data
        if ( value ) {
          sseBuffer += decoder.decode(value, { stream: true });
          const parts = sseBuffer.split('\n');
          sseBuffer = parts.pop() || '';
          for (const data of parts) {
            if ( !data.startsWith("data: ") ) continue;
            const line = data.substring(6).trim();
            if ( !line || line === '[DONE]' ) continue;
            let obj;
            try {
              obj = JSON.parse( line );
            } catch(parseError) {
              console.warn('[llamaSend] skip bad JSON line:', line.substring(0, 80));
              continue;
            }
            if ( obj && obj.error ) throw new Error( obj.error.message || ''+res.status );
            const piece = obj?.choices?.[0]?.delta?.content || '';
            if ( piece ) {
              node.dataset.output += piece;
              tts += piece;
              addText(node, piece);
            }
          }
        }

        // Поточная озвучка во время стрима
        if ( tts ) {
          let idx = 0;
          ['.','!','?','\n'].forEach( x => {
            let tmp = tts.lastIndexOf(x);
            if ( tmp > idx ) idx = tmp + 1;
          });
          if ( idx > 10 ) {
            let s = tts.substring(0, idx);
            if ( s && s.length ) {
              tts = tts.substring(idx).trimStart();
              if ( cfg('voice-type') === 'eleven' ) {
                await elevenSpeak( s + " ", node );
              } else if ( cfg('voice-type') === 'microsoft' ) {
                await microsoftSpeak( s + " ", node );
              } else if ( cfg('voice-type') === 'silero' ) {
                await sileroSpeakViaHead(s, node);
                sileroNode = node;
              } else {
                exclude = excludesProcess(s, exclude);
                await head.speakText( s, {
                  lipsyncLang: cfg('voice-lipsync-lang'),
                  ttsVoice: cfg('voice-google-id'),
                  ttsLang: cfg('voice-google-id').substring(0,5),
                  ttsRate: cfg('voice-google-rate'),
                  ttsPitch: cfg('voice-google-pitch')
                }, addText.bind(null,node), exclude.excludes );
              }
            }
          }
        }

        if (done) {
          if ( tts ) {
            if ( cfg('voice-type') === 'eleven' ) {
              await elevenSpeak( tts + " ", node );
            } else if ( cfg('voice-type') === 'microsoft' ) {
              await microsoftSpeak( tts + " ", node );
            } else if ( cfg('voice-type') === 'silero' ) {
              if (sileroAccumulatedText.trim())
                await sileroSpeak(sileroAccumulatedText.trim(), sileroNode || node);
            } else {
              exclude = excludesProcess(tts,exclude);
              await head.speakText( tts, {
                lipsyncLang: cfg('voice-lipsync-lang'),
                ttsVoice: cfg('voice-google-id'),
                ttsLang: cfg('voice-google-id').substring(0,5),
                ttsRate: cfg('voice-google-rate'),
                ttsPitch: cfg('voice-google-pitch')
              }, addText.bind(null,node), exclude.excludes );
            }
          }
          break;
        }
      }

    } else {
      console.error( await res.text() );
      throw new Error( json.error ? json.error.message : ''+res.status );
    }

  } catch (error) {
    if ( signal.aborted ) error = "aborted";
    console.error(error);
    addText( node, ' [' + error + ']');
  } finally {
    aiController = null; // Reset the AbortController instance
    if ( elevenSocket ) {
      elevenSpeak("",null);
    }
    microsoftSpeak(null);
    sileroReset();

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });
  }
}

// Send message to LM Studio (OpenAI-compatible local server)
async function lmstudioSendMessage(node,msgs) {

  // Create a new AbortController instance
  aiController = new AbortController();
  const signal = aiController.signal;

  // Chat completion
  try {
    const lmstudioUrl = cfg('ai-lmstudio-url') || 'http://localhost:1234';
    const lmstudioModel = cfg('ai-lmstudio-model') || 'gemma-3-4b-it';

    const sysMsgs = msgs.filter(m => m.role === 'system');
    console.log('[lmstudio] sending', msgs.length, 'msgs; system=', sysMsgs.length, 'sys0.len=', sysMsgs[0] ? sysMsgs[0].content.length : 0, 'cfg-sys=', (cfg('ai-openai-system')||'').length, 'DOM-sys=', (d3.select('[data-ai-openai-system]').node()?.value || '').length, 'model=', lmstudioModel);

    // Message body
    const body = {
      model: lmstudioModel,
      messages: msgs.map( x => {
        const {role,content,name} = x;
        return (name ? {role,content,name} : {role,content});
      }),
      temperature: parseFloat(cfg('ai-lmstudio-temperature')) || 0.7,
      max_tokens: parseInt(cfg('ai-lmstudio-output')) || 2000,
      top_p: parseFloat(cfg('ai-lmstudio-topp')) || 0.9,
      stream: true
    };

    const res = await fetch( lmstudioUrl + '/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: signal
    });

    if ( res.ok ) {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let tts = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split('\n');

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || !trimmed.startsWith('data: ')) continue;
          const data = trimmed.substring(6);
          if (data === '[DONE]') break;

          try {
            const json = JSON.parse(data);
            const content = json.choices?.[0]?.delta?.content;
            if (content) {
              addText(node, content);
              tts += content;
            }
          } catch(e) {}
        }

        // TTS streaming
        if ( tts ) {
          let idx = 0;
          ['.','!','?','\n'].forEach( x => {
            let tmp = tts.lastIndexOf(x);
            if ( tmp > idx ) idx = tmp + 1;
          });
          if ( idx > 10 ) {
            let s = tts.substring(0, idx);
            if ( s && s.length ) {
              tts = tts.substring(idx).trimStart();
              if ( cfg('voice-type') === 'eleven' ) {
                await elevenSpeak( s + " ", node );
              } else if ( cfg('voice-type') === 'microsoft' ) {
                await microsoftSpeak( s + " ", node );
        } else if ( cfg('voice-type') === 'silero' ) {
                await sileroSpeakViaHead(s, node);
                sileroNode = node;
              } else {
                exclude = excludesProcess(s,exclude);
                await head.speakText( s, {
                  lipsyncLang: cfg('voice-lipsync-lang'),
                  ttsVoice: cfg('voice-google-id'),
                  ttsLang: cfg('voice-google-id').substring(0,5),
                  ttsRate: cfg('voice-google-rate'),
                  ttsPitch: cfg('voice-google-pitch')
                }, addText.bind(null,node), exclude.excludes );
              }
            }
          }
        }
      }

    } else {
      console.error( await res.text() );
      throw new Error( ''+res.status );
    }

  } catch (error) {
    if ( signal.aborted ) error = "aborted";
    console.error(error);
    addText( node, ' [' + error + ']');
  } finally {
    aiController = null;
    if ( elevenSocket ) {
      elevenSpeak("",null);
    }
    microsoftSpeak(null);
    // Sync emotions from AI response
    if (node && node.dataset) {
      const fullText = node.dataset.markdown || '';
      if (fullText) playEmotionMotions(fullText);
    }

    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });
  }
}

// Build outgoing message for Google Gemini Pro
function geminiBuildMessage() {
  const msgs = [];

  const systems = [
    { sel: "[data-ai-gemini-system]", role: "system" },
    { sel: "[data-ai-gemini-user1]", role: "user" },
    { sel: "[data-ai-gemini-ai1]", role: "model" },
    { sel: "[data-ai-gemini-user2]", role: "user" },
    { sel: "[data-ai-gemini-ai2]", role: "model" }
  ];
  const session = d3.select("#right-sessions .session.selected");
  const input = d3.select("#input");
  const messages = session.selectAll(".message:not(.grayed)");

  const limitDialog = parseInt(cfg('ai-gemini-dialog')) || 4;
  const limitInput = parseInt(cfg('ai-gemini-input')) || 2000;
  const ctxPairs = parseInt(cfg('ai-context-pairs')) || 6;
  let dialogs = 0;
  let pairCount = 0;
  let words = 0;

  // System messages
  systems.forEach( x => {
    const n = d3.select(x.sel).node();
    if ( n.value && n.value.length ) {
      if ( n.dataset.words ) {
        words += parseInt( n.dataset.words );
      } else {
        let wc = nWords( n.value );
        n.dataset.words = wc;
        words += wc;
      }
      msgs.push( { n: n, role: x.role, content: n.value } );
    }
  });

  // messages in reverse order
  const revmsgs = [];
  revmsgs.push( { n: input.node(), role: "user", content: input.property("value") } );
  words += nWords( input.property("value") );
  for (const n of messages.nodes().reverse()) {
    if ( dialogs >= limitDialog || words >= limitInput || pairCount >= ctxPairs * 2 ) break;
    let role;
    let val;
    if ( n.dataset.input && n.dataset.input.length ) {
      role = "user";
      val = n.dataset.input;
    } else if ( n.dataset.output && n.dataset.output.length ) {
      role = "model";
      val = n.dataset.output;
    }
    if ( role && val ) {
      revmsgs.push( { n: n, role: role, content: val } );
      if ( n.dataset.words ) {
        words += parseInt( n.dataset.words );
      } else {
        let wc = nWords( val );
        n.dataset.words = wc;
        words += wc;
      }
      if ( role === "user" ) pairCount++;
      dialogs++;
    }
  }

  // Build message
  msgs.push( ...revmsgs.reverse() );

  return msgs;
}

// Send messages to Gemini API and handle streamed response
async function geminiSendMessage(node,msgs) {

  // Create a new AbortController instance
  aiController = new AbortController();
  const signal = aiController.signal;

  // Elements
  node.dataset.output = '';

  // Chat completion
  try {

    const sysMsgs = msgs.filter(m => m.role === 'system');
    console.log('[gemini] sending', msgs.length, 'msgs; system=', sysMsgs.length, 'sys0.len=', sysMsgs[0] ? sysMsgs[0].content.length : 0, 'cfg-sys=', (cfg('ai-openai-system')||'').length, 'DOM-sys=', (d3.select('[data-ai-openai-system]').node()?.value || '').length);

    // Message body
    const body = {
      contents: [],
      safetySettings: [
        { category: "HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold: "BLOCK_ONLY_HIGH" },
        { category: "HARM_CATEGORY_HARASSMENT", threshold: "BLOCK_ONLY_HIGH" },
        { category: "HARM_CATEGORY_HATE_SPEECH", threshold: "BLOCK_ONLY_HIGH" },
        { category: "HARM_CATEGORY_DANGEROUS_CONTENT", threshold: "BLOCK_ONLY_HIGH" }
      ],
      generationConfig: {
        temperature: cfg('ai-gemini-temperature'),
        maxOutputTokens: cfg('ai-gemini-output'),
        topP: cfg('ai-gemini-topp'),
        topK: cfg('ai-gemini-topk'),
        candidateCount: 1
      }
    };

    // Add messages
    // Make sure multiturn messages alternate between user and model
    let nextRole = 'user';
    msgs.forEach( x => {
      if ( x.role === "system" ) {
        body["systemInstruction"] = { "parts": [ { "text": x.content } ] };
      } else {
        if ( x.role !== nextRole ) {
          body.contents.push( { role: nextRole, "parts": { "text": "..." } } )
        }
        body.contents.push( { role: x.role, "parts": { "text": x.content } } );
        nextRole = x.role === 'user' ? 'model' : 'user';
      }
    });

    // Stop
    const stop = cfg('ai-gemini-stop');
    if ( stop && stop.length ) {
      body.generationConfig.stopSequences = [ stop ];
    }

    // Function calling
    const isMotionSelected = d3.select("#motion").classed("selected");
    if ( isMotionSelected ) {
      body.tools = [{
        functionDeclarations: [{
          "name": "move_body",
          "description": "Set the action, still pose, gesture and/or mood of your avatar's body in virtual world",
          "parameters": {
            "type": "object",
            "properties": {
              "action": {
                "type": "STRING",
                format: "enum",
                nullable: true,
                "enum": Object.keys(site.animations)
              },
              "stillpose": {
                "type": "STRING",
                format: "enum",
                nullable: true,
                "enum": Object.keys(site.poses)
              },
              "gesture": {
                "type": "STRING",
                format: "enum",
                nullable: true,
                "enum": Object.keys(site.gestures)
              },
              "mood": {
                "type": "STRING",
                format: "enum",
                nullable: true,
                "enum": Object.keys(head.animMoods)
              }
            },
            "required": []
          }
        }]
      }];
    }

    // Function loop
    let fn;
    do {

      fn = null;

      // Endpoint/proxy and authentication
      let url;
      let headers = { "Content-Type": "application/json; charset=utf-8" };
      const apikey = d3.select("#apikey-gemini").property("value");
      if ( apikey ) {
        url = geminiEndpoint + cfg("ai-model") + ':streamGenerateContent?alt=sse';
        headers["x-goog-api-key"] = apikey;
      } else {
        url = geminiProxy + cfg("ai-model") + ':streamGenerateContent?alt=sse';
        headers["Authorization"] = "Bearer " + await jwtGet()
      }

      // Fetch the response from the Vertex AI API
      const res = await fetch( url, {
        method: "POST",
        headers: headers,
        body: JSON.stringify(body),
        signal
      });

      if ( res.ok ) {

        // Read the response as a stream of data
        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let tts = "";
        let sseBuffer = '';

        while (true) {
          const { done, value } = await reader.read();

          // Extract text data
          if ( value ) {
            sseBuffer += decoder.decode(value, { stream: true });
            const parts = sseBuffer.split('\n');
            sseBuffer = parts.pop() || '';
            for (const data of parts) {
              if ( !data.startsWith("data: ") ) continue;
              let finish = 'STOP';
              try {
                const obj = JSON.parse( data.substring(6).trim() );
                const canditate = obj?.candidates?.[0];
                finish = canditate?.finishReason || 'STOP';
                fn = canditate?.content?.parts?.[0]?.functionCall;
                if ( !fn ) {
                  const piece = canditate?.content?.parts?.[0]?.text || '';
                  if ( piece ) {
                    node.dataset.markdown += piece;
                    node.dataset.output += piece;
                    tts += piece;
                    addText(node, piece);
                  }
                }
              } catch(parseError) {
                console.warn('[geminiSend] skip bad JSON:', data.substring(0, 80));
              }
              if ( finish !== 'STOP' ) {
                throw new Error("Stopped for " + finish + ".");
              }
            }
          }

          // Поточная озвучка во время стрима
          if ( !done ) {
            let ndx = 0;
            ['.','!','?','\n'].forEach( x => {
              let tmp = tts.lastIndexOf(x);
              if ( tmp > ndx ) ndx = tmp + 1;
            });
            if ( ndx > 10 ) {
              const sentence = tts.substring(0, ndx);
              tts = tts.substring(ndx).trimStart();
              if ( cfg('voice-type') === 'eleven' ) {
                await elevenSpeak( sentence + " ", node );
              } else if ( cfg('voice-type') === 'microsoft' ) {
                await microsoftSpeak( sentence + " ", node );
              } else if ( cfg('voice-type') === 'silero' ) {
                if (sileroAccumulatedText.trim()) {
                  await head.speakText(sileroAccumulatedText.trim(), {
                    lipsyncLang: cfg('voice-lipsync-lang'),
                    ttsVoice: avatarVoice(),
                    ttsLang: cfg('voice-silero-lang') || 'ru'
                  }, addText.bind(null, node || sileroNode), []);
                }
                sileroReset();
              } else {
                await head.speakText( sentence, {
                  lipsyncLang: cfg('voice-lipsync-lang'),
                  ttsVoice: cfg('voice-google-id'),
                  ttsLang: cfg('voice-google-id').substring(0,5),
                  ttsRate: cfg('voice-google-rate'),
                  ttsPitch: cfg('voice-google-pitch')
                }, addText.bind(null,node) );
              }
            }
          }

          if ( done ) {
            // Финальная озвучка остатка
            if ( tts ) {
              if ( cfg('voice-type') === 'eleven' ) {
                await elevenSpeak( tts + " ", node );
              } else if ( cfg('voice-type') === 'microsoft' ) {
                await microsoftSpeak( tts + " ", node );
              } else if ( cfg('voice-type') === 'silero' ) {
                if (sileroAccumulatedText.trim()) {
                  await head.speakText(sileroAccumulatedText.trim(), {
                    lipsyncLang: cfg('voice-lipsync-lang'),
                    ttsVoice: avatarVoice(),
                    ttsLang: cfg('voice-silero-lang') || 'ru'
                  }, addText.bind(null, sileroNode || node), []);
                }
                sileroReset();
              } else {
                await head.speakText( tts, {
                  lipsyncLang: cfg('voice-lipsync-lang'),
                  ttsVoice: cfg('voice-google-id'),
                  ttsLang: cfg('voice-google-id').substring(0,5),
                  ttsRate: cfg('voice-google-rate'),
                  ttsPitch: cfg('voice-google-pitch')
                }, addText.bind(null,node) );
              }
            }
            break;
          }
        }

      } else {
        console.error( await res.text() );
        throw new Error( json.error ? json.error.message : ''+res.status );
      }

      // Call function
      if ( fn ) {

        // Add function call to body
        body.contents.push( {
          role: "model",
          parts: [
            {
              functionCall: {
                name: "move_body",
                args: {
                  "action": fn.args?.["action"],
                  "stillpose": fn.args?.["stillpose"],
                  "gesture": fn.args?.["gesture"],
                  "mood": fn.args?.["mood"]
                }
              }
            }
          ]
        });

        // Call function
        try {
          motion(fn.args?.["action"], fn.args?.["stillpose"], fn.args?.["gesture"], fn.args?.["mood"]);
        } catch(motionError) {
          console.error(motionError);
        }

        // Add response
        body.contents.push({
          role: "function",
          parts: [
            {
              functionResponse: {
                name: "move_body",
                response: {
                  "status": "ok"
                }
              }
            }
          ]
        });

        // Instruct not to make another call
        body.toolConfig = { functionCallingConfig: { mode: "NONE" } };

      }

    } while( fn ); // Repeat, if this iteration was a function call

  } catch (error) {
    if ( signal.aborted ) error = "aborted";
    console.error(error);
    addText( node, ' [' + error + ']');
  } finally {
    aiController = null; // Reset the AbortController instance
    if ( elevenSocket ) {
      elevenSpeak("",null);
    }
    microsoftSpeak(null);
    sileroReset();

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });

    // When this marker has been reached, stop blinking
    head.speakMarker( () => {
      d3.selectAll('.blink').classed("blink",false);
          });
  }
}


// Add new message to UI
function addMessage(user = false) {
  const session = d3.select("#right-sessions .session.selected");
  const message = session.append("div");
  message.classed("message", true)
    .classed("user",user)
    .classed("blink",!user);

  // Add selection
  const toolbarLeft = message.append("div");
  toolbarLeft.classed("toolbar-left", true);
  toolbarLeft.append("div")
    .classed("command",true)
    .classed("select", true )
    .on('click', function(ev) {
      let e = d3.select(this);
      let mode = !e.classed("selected");
      e.classed("selected", mode );
      d3.select(this.parentNode.parentNode).classed("grayed", mode );
    })
    .html(svgSelect);

  if ( user ) {
    const toolbarRight = message.append("div");
    toolbarRight.classed("toolbar-right", true);
    toolbarRight.append("div")
      .classed("command", true)
      .on('click', function(ev) {
        let n = this.parentNode.parentNode;
        const text = d3.select(n).attr('data-input');
        while(n) {
          let e = d3.select(n);
          e.classed("grayed",true);
          e.select(".select").classed("selected", true );
          n = n.nextSibling;
        }
        let node = d3.select("#input").property("value",text).dispatch("change").node();
        node.scrollTop = node.scrollHeight;
        node.focus();
      })
      .html(svgRepost);

  } else {
    const toolbarRight = message.append("div");
    toolbarRight.classed("toolbar-right", true);
    toolbarRight.append("div")
      .classed("command", true)
      .on('click', function(ev) {
        const e = d3.select(this.parentNode.parentNode);
        let text = e.attr("data-output");
        if ( text && text.length ) {
          if ( cfg('voice-type') === 'eleven' ) {
            elevenSpeak( text + " " );
            elevenSpeak( "" );
          } else if ( cfg('voice-type') === 'microsoft' ) {
            microsoftSpeak( text );
            microsoftSpeak( null );
          } else {
            head.speakText( text, {
              lipsyncLang: cfg('voice-lipsync-lang'),
              ttsVoice: cfg('voice-google-id'),
              ttsLang: cfg('voice-google-id').substring(0,5),
              ttsRate: cfg('voice-google-rate'),
              ttsPitch: cfg('voice-google-pitch')
            } );
          }
        }
      })
      .html(svgSpeak);

    toolbarRight.append("div")
      .classed("command", true)
      .on('click', function(ev) {
        head.stopSpeaking();
        // Abort the fetch request by calling abort() on the AbortController instance
        if (aiController) {
          aiController.abort();
          aiController = null;
        }
      })
      .html(svgStop);
  }

  message.append('p');

  const nodeSessions = d3.select("#right-sessions").node();
  if (nodeSessions) nodeSessions.scrollTop = nodeSessions.scrollHeight;

  // Add to floating chat panel
  if (window.addMessageToFloatingChat) window.addMessageToFloatingChat('', user);

  return message.node();
}

// Add text to session
function addText(node,s) {
  const nodeSessions = d3.select("#right-sessions").node();
  const onbottom = Math.abs( nodeSessions.scrollHeight - nodeSessions.scrollTop - nodeSessions.clientHeight) < 20;
  let last = node.lastElementChild;
  let markdown = (last.dataset.markdown || '') + s;
  let ndx = markdown.lastIndexOf('\n\n');
  if ( ndx === -1 ) {
    // Add to the existing paragraph
    last.innerHTML = dompurify.sanitize(marked.parseInline( markdown, markedOptions ));
    last.dataset.markdown = markdown;
  } else {
    // First part into the existing paragraph
    let md = markdown.substring(0, ndx);
    last.innerHTML = dompurify.sanitize(marked.parseInline( md, markedOptions ));
    last.dataset.markdown = md;

    // Last part into a new paragraph
    md = markdown.substring(ndx + 2);
    last = node.appendChild( document.createElement('p') );
    last.innerHTML = dompurify.sanitize(marked.parseInline( md, markedOptions ));
    last.dataset.markdown = md;
  }
  if ( onbottom ) nodeSessions.scrollTop = nodeSessions.scrollHeight;

  // Update floating chat panel — mirror all <p>s from main node
  if (window.updateFloatingChatLastMessage && node) {
    window.updateFloatingChatLastMessage(node);
  }
}

// ========== EMOTION SYNC: detect emotions in text and play motions ==========
const emotionMap = {
  // Emoji → motion name
  '😀': 'happy', '😃': 'happy', '😄': 'happy', '😁': 'grin', '😊': 'warm_smile',
  '😂': 'laugh', '🤣': 'laugh', '😹': 'laugh',
  '😍': 'heart_eyes', '😘': 'blow_kiss', '😗': 'kiss', '💋': 'kiss',
  '😢': 'sad_frown', '😭': 'sobbing', '😞': 'disappointed',
  '😠': 'rage', '😡': 'rage', '🤬': 'scream',
  '😱': 'surprised', '😲': 'surprised', '😮': 'open_mouth',
  '🤔': 'thinking_face', '🤨': 'side_glance',
  '😴': 'sleep', '💤': 'sleep',
  '😏': 'smug', '😎': 'smirk',
  '🙄': 'eyeroll', '😒': 'unamused',
  '😳': 'flushed', '🥺': 'pleading',
  '🤗': 'beam', '🥰': 'adoring',
  '😤': 'grimace', '🤢': 'disgust',
  '😨': 'fear', '😰': 'nervous',
  '👋': 'wave_right', '🤚': 'hand_raise', '✋': 'hand_raise',
  '👍': 'thumbs_up', '👎': 'thumbs_down',
  '👌': 'ok_sign', '🤝': 'ok_sign',
  '🙏': 'pray', '🤲': 'pray',
  '👏': 'applause', '🙌': 'celebrate',
  '💪': 'excited', '🤩': 'excited',
  '😉': 'wink', '😜': 'wink_smile',
  '🤪': 'tongueout', '😛': 'tongueout',
  '😐': 'neutral_face', '😶': 'neutral_face',
  '🙃': 'smirk', '😬': 'grimace_teeth',
  '🤭': 'laugh_closed', '😆': 'open_grin',
  '💀': 'grimace', '☠️': 'grimace',
  '❤️': 'love', '💕': 'love', '💖': 'love',
  '🔥': 'excited', '✨': 'celebrate',
  '🎉': 'celebrate', '🎊': 'celebrate',
  '🤷': 'shrug_both', '🤷‍♂️': 'shrug_both', '🤷‍♀️': 'shrug_both',
  '😅': 'slight_smile', '🙂': 'slight_smile',
  '😔': 'pensive', '😌': 'sigh',
  '🤮': 'disgust', '🤧': 'squint',
  '🥴': 'dizzy', '😵': 'dizzy',
  '🤯': 'surprised', '😶‍🌫️': 'thinking_face',
  '🫡': 'bow', '🙇': 'bow', '🙇‍♂️': 'bow',
  '💃': 'dance', '🕺': 'dance',
  '🏃': 'jump', '🤸': 'jump',
  '👊': 'fist', '✊': 'fist',
  '✌️': 'peace', '🤞': 'pray',
  '🤙': 'hand_raise', '👈': 'point', '👉': 'point', '👆': 'point', '👇': 'point',
  '🫠': 'sad_frown', '🥲': 'warm_smile', '😇': 'beam',
  '🤓': 'thinking_face', '🧐': 'curious',
  '😪': 'yawn', '🥱': 'yawn',
  '🤥': 'smirk', '🫣': 'squint',
  '😷': 'grimace', '🤒': 'sad_frown', '🤕': 'sad_frown',
  '🥶': 'shiver', '🥵': 'flushed',
  '😫': 'frown', '😩': 'frown',
  '🫤': 'unamused', '🫥': 'neutral_face',
  '😸': 'happy', '😺': 'happy', '😻': 'heart_eyes',
  '🙈': 'close_eyes', '🙉': 'close_eyes', '🙊': 'close_eyes',
  '🐱': 'happy', '😺': 'happy',
  // Text keywords → motion
  'haha': 'laugh', 'lol': 'laugh', 'lmao': 'laugh', 'rofl': 'laugh',
  'wow': 'surprised', 'omg': 'surprised', 'whoa': 'surprised',
  'hi': 'wave_right', 'hello': 'wave_right', 'hey': 'wave_right',
  'bye': 'wave_right', 'goodbye': 'wave_right',
  'thanks': 'bow', 'thank': 'bow', 'thx': 'bow',
  'sorry': 'pleading', 'apologize': 'pleading',
  'congratulations': 'celebrate', 'congrats': 'celebrate', 'bravo': 'applause',
  'yes': 'nod_yes', 'yeah': 'nod_yes', 'yep': 'nod_yes', 'sure': 'nod_yes',
  'no': 'shake_no', 'nope': 'shake_no', 'nah': 'shake_no',
  'love': 'love', 'adore': 'adore', 'heart': 'love',
  'angry': 'rage', 'mad': 'rage', 'furious': 'rage',
  'sad': 'sad_frown', 'cry': 'sobbing', 'crying': 'sobbing',
  'scared': 'fear', 'afraid': 'fear', 'terrified': 'fear',
  'sleepy': 'sleep', 'tired': 'sleep', 'exhausted': 'sleep',
  'think': 'thinking_face', 'hmm': 'thinking_face', 'wonder': 'thinking_face',
  'shrug': 'shrug_both', 'dunno': 'shrug_both', 'whatever': 'dismiss',
  'wink': 'wink', 'winky': 'wink_smile',
  'kiss': 'kiss', 'mwah': 'kiss',
  'dance': 'dance', 'party': 'celebrate',
  'clap': 'applause', 'bravo': 'applause',
  'pray': 'pray', 'amen': 'pray',
  'thumbsup': 'thumbs_up', 'thumbsdown': 'thumbs_down',
  'ok': 'ok_sign', 'okay': 'ok_sign',
  'peace': 'namaste_bow', 'namaste': 'namaste_bow',
  'point': 'point', 'look': 'side_glance',
  'yawn': 'yawn', 'bored': 'yawn',
  'shiver': 'shiver', 'cold': 'shiver',
  'nervous': 'nervous', 'anxious': 'nervous',
  'shy': 'shy', 'blush': 'shy',
  'smirk': 'smirk', 'smug': 'smug',
  'grimace': 'grimace', 'gross': 'disgust',
  'disgust': 'disgust', 'eww': 'disgust', 'yuck': 'disgust',
  'frown': 'frown', 'unhappy': 'frown',
  'squint': 'squint', 'suspicious': 'squint',
  'curious': 'curious', 'interested': 'curious',
  'surprise': 'surprised', 'shocked': 'surprised',
  'smile': 'warm_smile', 'grin': 'grin', 'beam': 'beam',
  'laugh': 'laugh', 'giggle': 'laugh_closed',
  'cry': 'sobbing', 'sob': 'sobbing',
  'sigh': 'sigh', 'relief': 'sigh',
  'pensive': 'pensive', 'thoughtful': 'pensive',
  'disappointed': 'disappointed', 'letdown': 'disappointed',
  'puppy': 'puppy_eyes', 'beg': 'pleading',
  'facepalm': 'facepalm', 'doh': 'facepalm',
  'scream': 'scream', 'yell': 'scream',
  'rage': 'rage', 'furious': 'rage',
  'flushed': 'flushed', 'embarrassed': 'flushed',
  'chew': 'chew', 'eat': 'chew',
  'vibrate': 'vibrate', 'buzz': 'vibrate',
  'headcircle': 'head_circles', 'dizzy': 'head_circles',
  'turn': 'turn_around', 'spin': 'turn_around',
  'jump': 'jump', 'hop': 'jump',
  'deepbreath': 'deep_breath', 'breathe': 'deep_breath',
  'zzz': 'sleep', 'snore': 'sleep',
  'closeeyes': 'close_eyes', 'blink': 'close_eyes',
  'tongue': 'tongueout', 'tongue_out': 'tongueout',
  'raiseeyebrows': 'raise_eyebrows', 'surprised': 'raise_eyebrows',
  'openmouth': 'open_mouth', 'gasp': 'open_mouth',
  'cheekpuff': 'cheek_puff', 'puff': 'cheek_puff',
  'wink_smile': 'wink_smile', 'winksmile': 'wink_smile',
  'laugh_closed': 'laugh_closed', 'laughclosed': 'laugh_closed',
  'crying_laugh': 'crying_laugh', 'tears_of_joy': 'crying_laugh',
  'squint_smile': 'squint_smile', 'squintsmile': 'squint_smile',
  'adoring': 'adoring', 'admire': 'adoring',
  'kiss_eyes': 'kiss_eyes_closed', 'kiss_eyes_closed': 'kiss_eyes_closed',
  'blow_kiss': 'blow_kiss', 'blowkiss': 'blow_kiss',
  'ok_wink': 'ok_wink', 'okwink': 'ok_wink',
  'shrug_confused': 'shrug_confused', 'confused': 'shrug_confused',
  'namaste_bow': 'namaste_bow', 'bow': 'bow',
  'nod': 'nod', 'nod_yes': 'nod_yes',
  'head_shake': 'head_shake', 'shake_no': 'shake_no',
  'look_up': 'look_up', 'lookup': 'look_up',
  'look_down': 'look_down', 'lookdown': 'look_down',
  'look_left': 'look_left', 'lookleft': 'look_left',
  'look_right': 'look_right', 'lookright': 'look_right',
  'hand_raise': 'hand_raise', 'raisehand': 'hand_raise',
  'hand_raise_left': 'hand_raise_left', 'raisehandleft': 'hand_raise_left',
  'thumbup_right': 'thumbs_up', 'thumbup': 'thumbs_up',
  'thumbdown_right': 'thumbs_down', 'thumbdown': 'thumbs_down',
};

function detectEmotions(text) {
  const motions = [];
  const lower = text.toLowerCase();
  // Check emoji
  for (const [emoji, motion] of Object.entries(emotionMap)) {
    if (emoji.length > 3) continue; // skip text keywords here
    if (text.includes(emoji) && !motions.includes(motion)) {
      motions.push(motion);
    }
  }
  // Check text keywords
  const words = lower.split(/\s+/);
  for (const word of words) {
    const clean = word.replace(/[^a-z]/g, '');
    if (emotionMap[clean] && !motions.includes(emotionMap[clean])) {
      motions.push(emotionMap[clean]);
    }
  }
  return motions;
}

function playEmotionMotions(text) {
  if (!motionEngine) return;
  const motions = detectEmotions(text);
  if (motions.length > 0) {
    // Play first detected emotion, queue others
    motionEngine.play(motions[0]);
    for (let i = 1; i < motions.length; i++) {
      setTimeout(() => {
        if (motionEngine) motionEngine.play(motions[i]);
      }, i * 1500);
    }
  }
}

// External speak
async function speak(s) {
  let node = addMessage(false);
  if ( cfg('voice-type') === 'eleven' ) {
    await elevenSpeak( s, node );
    await elevenSpeak( "", node );
  } else if ( cfg('voice-type') === 'microsoft' ) {
    await microsoftSpeak( s, node );
    await microsoftSpeak(null);
  } else if ( cfg('voice-type') === 'silero' || (head && head.opt && head.opt.ttsEngine === 'silero') ) {
    /* Аватар инициализирован с Silero (/api/tts). Раньше этот путь уходил в
       ветку Google: идентификатор голоса Google в роли спикера Silero не
       существует, и сервер озвучивал ответ подставным мужским голосом. */
    await sileroSpeakViaHead( s, node );
  } else {
    await head.speakText( s, {
      lipsyncLang: cfg('voice-lipsync-lang'),
      ttsVoice: cfg('voice-google-id'),
      ttsLang: cfg('voice-google-id').substring(0,5),
      ttsRate: cfg('voice-google-rate'),
      ttsPitch: cfg('voice-google-pitch')
    }, addText.bind(null,node) );
  }

  // When this marker has been reached, stop blinking
  head.speakMarker( () => {
    d3.selectAll('.blink').classed("blink",false);
  });

  // Sync emotions from text
  playEmotionMotions(s);
}
window.talkingheadSpeak = speak;

// Select an directory entry
function entrySelect() {
  reconnectEffect();
  const e = d3.select(this);
  const id = parseInt( e.attr("data-session") );
  d3.selectAll("[data-session]").classed('selected',false);
  e.classed('selected',true);
  loadConfig(id);
  i18nTranslate();
  d3.selectAll("[data-range]").dispatch("change");
}

// Move directory entry
function entryMove() {
  const e = d3.select(this);
  const n = e.node().parentElement; // Entry
  const id = parseInt( n.id.split('-')[1] );
  const directory = d3.select("#directory").node();
  const direction = e.attr("data-entry-move");
  let session = CFG.session;

  if ( direction === 'up' ) {

    // Swap configuration
    var tmp = CFG.sessions[id];
    CFG.sessions[id] = CFG.sessions[id-1];
    CFG.sessions[id-1] = tmp;

    // Swap directory entries
    d3.select(n)
      .property("id","entry-"+(id-1))
      .select("[data-session]")
      .attr("data-session",(id-1));
    d3.select(n.previousElementSibling)
      .property("id","entry-"+id)
      .select("[data-session]")
      .attr("data-session",id);
    directory.insertBefore(n,n.previousElementSibling);
    if ( session === id ) {
      session = id - 1;
    } else if ( session === (id-1) ) {
      session = id;
    }

    // Swap sessions ids (no need to swap their places)
    const s1 = d3.select("#session-"+id);
    const s2 = d3.select("#session-"+(id-1));
    s1.property("id","session-"+(id-1) );
    s2.property("id","session-"+id );

  } else if ( direction === 'down' ) {

    // Swap configuration
    var tmp = CFG.sessions[id];
    CFG.sessions[id] = CFG.sessions[id+1];
    CFG.sessions[id+1] = tmp;

    // Swap directory entries
    d3.select(n)
      .property("id","entry-"+(id+1))
      .select("[data-session]")
      .attr("data-session",(id+1));
    d3.select(n.nextElementSibling)
      .property("id","entry-"+id)
      .select("[data-session]")
      .attr("data-session",id);
    directory.insertBefore(n.nextElementSibling,n);
    if ( session === id ) {
      session = id + 1;
    } else if ( session === (id+1) ) {
      session = id;
    }

    // Swap sessions ids (no need to swap their places)
    const s1 = d3.select("#session-"+id);
    const s2 = d3.select("#session-"+(id+1));
    s1.property("id","session-"+(id+1) );
    s2.property("id","session-"+id );

  }

  // Save new configuration and load the session
  saveConfig();
  loadConfig(session);
  i18nTranslate();
  d3.selectAll("[data-range]").dispatch("change");
}

// Turn on effect
function turnOnEffect() {
  d3.selectAll('.starttransparent').nodes().forEach( panel => {
    const steps = 5 + Math.floor( Math.random() * 10 );
    const ops = [];
    for( let i=steps; i > 0; i-- ) {
      ops.push( { opacity: Math.random() } );
    }
    ops.push( { opacity: 1 } );
    const timing = {
      duration: Math.floor( Math.random() * 2000 ) + 300,
      fill: "forwards"
    };
    panel.animate(ops,timing);
  });
}

// Reconnect effect
function reconnectEffect() {

  // Turn off
  d3.selectAll("#avatar,#view").style("opacity","0");
  d3.select("#left").classed("glow",false);

  // Turn on
  setTimeout( () => {
    d3.selectAll("#avatar,#view").style("opacity","1");
    d3.select("#left").classed("glow",true);
  }, 1000);

}



// ========== FLOATING PANELS JS (WinBox) ==========
(function(){
  var btnChat = document.getElementById('btn-chat');
  var btnSettings = document.getElementById('btn-settings');
  var btnBoneControl = document.getElementById('bonecontroltoggle');
  var chatPanel = document.getElementById('chat-panel');
  var settingsPanel = document.getElementById('settings-panel');
  var boneControlPanel = document.getElementById('bonecontrolpanel');
  var chatMessages = document.getElementById('chat-panel-messages');
  var chatInput = document.getElementById('chat-panel-input');
  var settingsContent = document.getElementById('settings-panel-content');
  var chatOpen = false, settingsOpen = false;
  var tools = document.getElementById('chat-panel-tools');
  var chatWinBox = null, settingsWinBox = null;
  var chatPanelParent = chatPanel ? chatPanel.parentNode : null;
  var settingsPanelParent = settingsPanel ? settingsPanel.parentNode : null;

  function restoreSettingsPages() {
    if (settingsContent && settingsContent.hasChildNodes()) {
      var pages = settingsContent.querySelector('.pages');
      if (pages) {
        var right = document.getElementById('right');
        if (right) {
          right.appendChild(pages);
          var sessionsPage = right.querySelector('#right-sessions');
          if (sessionsPage && !pages.contains(sessionsPage)) {
            pages.insertBefore(sessionsPage, pages.firstChild);
          }
        }
      }
    }
  }

  function closeChatWinBox() {
    if (chatWinBox) {
      chatWinBox.close();
      chatWinBox = null;
    }
  }

  function closeSettingsWinBox() {
    if (settingsWinBox) {
      settingsWinBox.close();
      settingsWinBox = null;
    }
  }

  // Toggle chat panel
  function toggleChat() {
    chatOpen = !chatOpen;
    btnChat.classList.toggle('active', chatOpen);
    if (chatOpen) {
      settingsOpen = false;
      btnSettings.classList.remove('active');
      closeSettingsWinBox();
      if (chatWinBox) closeChatWinBox();

      chatPanel.classList.remove('hidden');
      var chatMessages = document.getElementById('chat-panel-messages');
      var chatHeader = chatPanel.querySelector('.floating-panel-header');
      var chatTools = document.querySelector('.chat-tools-row');
      var toolsH = chatTools ? (chatTools.offsetHeight || chatTools.parentElement.offsetHeight || 40) : 40;
      var headerH = chatHeader ? chatHeader.offsetHeight : 36;
      var msgH = chatMessages ? Math.max(chatMessages.scrollHeight, 100) : 100;
      var chatContentH = msgH + headerH + toolsH + 8;
      var winH = Math.min(chatContentH, window.innerHeight * 0.85);
      chatWinBox = new WinBox('Chat', {
        width: '480px',
        height: winH + 'px',
        max: false,
        minHeight: '180px',
        maxHeight: Math.floor(window.innerHeight * 0.85) + 'px',
        x: 'center',
        y: 'center',
        mount: chatPanel,
        class: 'wb-chat',
        onclose: function() {
          chatOpen = false;
          btnChat.classList.remove('active');
          if (chatPanelParent) chatPanelParent.appendChild(chatPanel);
          chatPanel.classList.add('hidden');
          chatWinBox = null;
        }
      });
      var input = document.getElementById('input');
      if (input) input.focus();
    } else {
      closeChatWinBox();
    }
  }

  // Toggle settings panel
  function toggleSettings() {
    settingsOpen = !settingsOpen;
    btnSettings.classList.toggle('active', settingsOpen);
    if (settingsOpen) {
      chatOpen = false;
      btnChat.classList.remove('active');
      closeChatWinBox();
      if (settingsWinBox) closeSettingsWinBox();

      // Move #right pages into settings panel (not clone — preserves D3 event handlers)
      var right = document.getElementById('right');
      if (settingsContent && right) {
        if (settingsContent.children.length === 0) {
          var pages = right.querySelector('.pages');
          if (pages) {
            var sessionsPage = pages.querySelector('#right-sessions');
            if (sessionsPage) right.appendChild(sessionsPage);
            settingsContent.appendChild(pages);
          }
        }
        if (settingsContent.children.length === 0) {
          pages = right.querySelector('.pages');
          if (!pages) {
            var allPages = right.children;
            for (var i = 0; i < allPages.length; i++) {
              settingsContent.appendChild(allPages[i]);
            }
          }
        }
      }

      settingsPanel.classList.remove('hidden');
      var settingsContentEl = document.getElementById('settings-panel-content');
      var settingsHeader = settingsPanel.querySelector('.floating-panel-header');
      var pagesEl = settingsContentEl ? settingsContentEl.querySelector('.pages') : null;
      var pagesH = pagesEl ? pagesEl.scrollHeight : 300;
      var settingsContentH = pagesH + (settingsHeader ? settingsHeader.offsetHeight : 36) + 4;
      var winH = Math.min(settingsContentH, window.innerHeight * 0.85);
      /* Сама панель настроек — 500px (portable.css). Окно в 480px обрезало её:
         подписи строк уезжали за левый край, правый край прятался. Берём
         ширину содержимого и не выходим за пределы окна приложения. */
      var panelW = settingsPanel.offsetWidth || 500;
      var winW = Math.min(panelW + 20, Math.floor(window.innerWidth * 0.92));
      settingsWinBox = new WinBox('Settings', {
        width: winW + 'px',
        height: winH + 'px',
        max: false,
        minHeight: '250px',
        maxHeight: Math.floor(window.innerHeight * 0.85) + 'px',
        x: 'center',
        y: 'center',
        mount: settingsPanel,
        class: 'wb-settings',
        onclose: function() {
          settingsOpen = false;
          btnSettings.classList.remove('active');
          restoreSettingsPages();
          if (settingsPanelParent) settingsPanelParent.appendChild(settingsPanel);
          settingsPanel.classList.add('hidden');
          settingsWinBox = null;
        }
      });
    } else {
      closeSettingsWinBox();
    }
  }

  btnChat.addEventListener('click', toggleChat);
  btnSettings.addEventListener('click', toggleSettings);
  document.getElementById('chat-panel-close').addEventListener('click', closeChatWinBox);
  document.getElementById('settings-panel-close').addEventListener('click', closeSettingsWinBox);
  var boneCloseBtn = document.getElementById('bonecontrol-close');
  if (boneCloseBtn) {
    boneCloseBtn.addEventListener('click', function() {
      var panel = document.getElementById('bonecontrolpanel');
      var toggle = document.getElementById('bonecontroltoggle');
      if (panel) panel.classList.add('hidden');
      if (toggle) toggle.classList.remove('active');
    });
  }

  // Chat input: sync to #input
  if (chatInput) {
    chatInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        var text = chatInput.value.trim();
        if (!text) return;
        var mainInput = document.getElementById('input');
        if (mainInput) {
          mainInput.value = text;
          mainInput.dispatchEvent(new Event('change'));
          mainInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, bubbles: true }));
        }
        chatInput.value = '';
        chatInput.style.height = 'auto';
      }
    });
    chatInput.addEventListener('input', function() {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 80) + 'px';
    });
  }

  // Chat panel buttons: delegate clicks to original #bottom buttons
  if (tools) {
    tools.addEventListener('click', function(e) {
      e.preventDefault();
      e.stopPropagation();
      var btn = e.target.closest('.command, .text');
      if (!btn) return;
      var targetId = btn.getAttribute('data-target');
      if (!targetId) return;
      var origBtn = document.getElementById(targetId);
      if (origBtn) {
        origBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      }
    });
  }

  // Close on outside click (only for bone control, WinBox panels close via their own button)
  document.addEventListener('click', function(e) {
    if (boneControlPanel && !boneControlPanel.classList.contains('hidden')) {
      if (!boneControlPanel.contains(e.target) && !btnBoneControl.contains(e.target)) {
        boneControlPanel.classList.add('hidden');
        btnBoneControl.classList.remove('active');
      }
    }
  });

  // Expose: add message to floating chat panel
  window.addMessageToFloatingChat = function(text, isUser) {
    if (!chatMessages) return;
    var session = chatMessages.querySelector('.session.selected');
    if (!session) {
      session = document.createElement('div');
      session.className = 'session selected';
      chatMessages.appendChild(session);
    }
    var msg = document.createElement('div');
    msg.className = 'message' + (isUser ? ' user' : '');
    var p = document.createElement('p');
    p.textContent = text;
    msg.appendChild(p);
    session.appendChild(msg);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  };

  // Expose: update last message in floating chat panel — mirror all <p>s from main node
  window.updateFloatingChatLastMessage = function(mainNode) {
    if (!chatMessages) return;
    var session = chatMessages.querySelector('.session.selected');
    if (!session) return;
    var msgs = session.querySelectorAll('.message');
    if (!msgs.length) return;
    var floatLast = msgs[msgs.length-1];
    if (!floatLast) return;
    // Собираем все <p> из mainNode
    var ps = mainNode ? mainNode.querySelectorAll('p') : [];
    // Удаляем все текущие <p> во floating panel
    var existingPs = floatLast.querySelectorAll('p');
    for (var i = existingPs.length - 1; i >= 0; i--) {
      existingPs[i].remove();
    }
    // Клонируем все <p> из mainNode
    if (ps && ps.length) {
      for (var j = 0; j < ps.length; j++) {
        floatLast.appendChild(ps[j].cloneNode(true));
      }
    } else {
      var p = document.createElement('p');
      p.textContent = '';
      floatLast.appendChild(p);
    }
    chatMessages.scrollTop = chatMessages.scrollHeight;
  };

  // ========== DRAGGABLE PANELS ==========
  function makeDraggable(panel, storageKey) {
    if (!panel) return;

    var saved = localStorage.getItem(storageKey);
    if (saved) {
      try {
        var pos = JSON.parse(saved);
        if (pos.top) panel.style.top = pos.top;
        if (pos.left) panel.style.left = pos.left;
        panel.style.right = 'auto';
      } catch(e) {}
    }

    var isDragging = false;
    var startX, startY, startLeft, startTop;
    var rafId = null;

    function handleMove(e) {
      if (!isDragging) return;
      var dx = e.clientX - startX;
      var dy = e.clientY - startY;
      var newLeft = startLeft + dx;
      var newTop = startTop + dy;
      var rect = panel.getBoundingClientRect();
      newLeft = Math.max(0, Math.min(newLeft, window.innerWidth - rect.width));
      newTop = Math.max(0, Math.min(newTop, window.innerHeight - rect.height));
      panel.style.left = newLeft + 'px';
      panel.style.top = newTop + 'px';
      panel.style.right = 'auto';
    }

    function handleMoveRAF(e) {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        handleMove(e);
        rafId = null;
      });
    }

    function handleUp() {
      if (!isDragging) return;
      isDragging = false;
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      panel.style.transition = '';
      var rect = panel.getBoundingClientRect();
      localStorage.setItem(storageKey, JSON.stringify({
        top: rect.top + 'px',
        left: rect.left + 'px'
      }));
      document.removeEventListener('mousemove', handleMoveRAF);
      document.removeEventListener('mouseup', handleUp);
    }

    panel.addEventListener('mousedown', function(e) {
      if (e.target.closest('.command') || e.target.closest('textarea') || e.target.closest('input') || e.target.closest('.floating-panel-close') || e.target.closest('.chat-tools-row')) return;
      isDragging = true;
      startX = e.clientX;
      startY = e.clientY;
      var rect = panel.getBoundingClientRect();
      startLeft = rect.left;
      startTop = rect.top;
      panel.style.transition = 'none';
      e.preventDefault();
      e.stopPropagation();
      document.addEventListener('mousemove', handleMoveRAF);
      document.addEventListener('mouseup', handleUp);
    });
  }

  function positionPanel(panel, btn) {
    if (!panel || !btn) return;
    var btnRect = btn.getBoundingClientRect();
    var panelW = panel.offsetWidth || 320;
    var left = btnRect.right + 10;
    if (left + panelW > window.innerWidth) left = btnRect.left - panelW - 10;
    panel.style.left = Math.max(4, left) + 'px';
    panel.style.top = Math.max(4, btnRect.top) + 'px';
    panel.style.right = 'auto';
  }

  makeDraggable(boneControlPanel, 'floating-bonecontrol-position');

  // ========== BONE CONTROLLER ==========
  let _boneName = '';

  function selectBone(name) {
    _boneName = name || '';
    d3.selectAll("#bonecontrol-list > .bonecontrol-item").classed("selected", function() {
      return d3.select(this).attr("data-bone") === _boneName;
    });
    if (!_boneName || !head || !head.armature) return;
    let bone = null;
    head.armature.traverse((obj) => {
      if (obj.isBone && obj.name === _boneName) bone = obj;
    });
    if (!bone) return;
    const rx = THREE.MathUtils.radToDeg(bone.rotation.x);
    const ry = THREE.MathUtils.radToDeg(bone.rotation.y);
    const rz = THREE.MathUtils.radToDeg(bone.rotation.z);
    d3.select("#bonecontrolrotx").property("value", rx);
    d3.select("#bonecontrolroty").property("value", ry);
    d3.select("#bonecontrolrotz").property("value", rz);
    d3.select("#bonecontrolrotxval").text(Math.round(rx) + "°");
    d3.select("#bonecontrolrotyval").text(Math.round(ry) + "°");
    d3.select("#bonecontrolrotzval").text(Math.round(rz) + "°");
    d3.select("#bonecontrolposx").property("value", bone.position.x);
    d3.select("#bonecontrolposy").property("value", bone.position.y);
    d3.select("#bonecontrolposz").property("value", bone.position.z);
    d3.select("#bonecontrolposxval").text(bone.position.x.toFixed(2));
    d3.select("#bonecontrolposyval").text(bone.position.y.toFixed(2));
    d3.select("#bonecontrolposzval").text(bone.position.z.toFixed(2));
    d3.select("#bonecontrolscalex").property("value", bone.scale.x);
    d3.select("#bonecontrolscaley").property("value", bone.scale.y);
    d3.select("#bonecontrolscalez").property("value", bone.scale.z);
    d3.select("#bonecontrolscalexval").text(bone.scale.x.toFixed(2));
    d3.select("#bonecontrolscaleyval").text(bone.scale.y.toFixed(2));
    d3.select("#bonecontrolscalezval").text(bone.scale.z.toFixed(2));
  }

  window.toggleBonePanel = function() {
    const panelEl = document.getElementById('bonecontrolpanel');
    const toggleEl = document.getElementById('bonecontroltoggle');
    if (!panelEl) { console.warn('[BONE] panel element not found'); return; }
    const isHidden = panelEl.classList.contains('hidden');
    panelEl.classList.toggle('hidden');
    if (toggleEl) toggleEl.classList.toggle('active');
    console.log('[BONE] toggle hidden:', isHidden);
    if (isHidden) {
      // Position panel relative to toggle button, fallback to center
      if (toggleEl) {
        const btnRect = toggleEl.getBoundingClientRect();
        if (btnRect.width > 0 && btnRect.height > 0) {
          const panelW = panelEl.offsetWidth || 320;
          const left = Math.min(btnRect.right + 10, window.innerWidth - panelW - 10);
          const top = Math.max(4, btnRect.top);
          panelEl.style.left = Math.max(4, left) + 'px';
          panelEl.style.top = top + 'px';
          panelEl.style.right = 'auto';
        } else {
          // Button not visible — center on screen
          panelEl.style.left = Math.max(4, (window.innerWidth - (panelEl.offsetWidth || 320)) / 2) + 'px';
          panelEl.style.top = Math.max(4, (window.innerHeight - (panelEl.offsetHeight || 400)) / 2) + 'px';
          panelEl.style.right = 'auto';
        }
      } else {
        panelEl.style.left = Math.max(4, (window.innerWidth - (panelEl.offsetWidth || 320)) / 2) + 'px';
        panelEl.style.top = Math.max(4, (window.innerHeight - (panelEl.offsetHeight || 400)) / 2) + 'px';
        panelEl.style.right = 'auto';
      }
      // Populate bones
      setTimeout(() => {
        if (head && head.armature) {
          const list = d3.select("#bonecontrol-list");
          list.selectAll("*").remove();

          const categories = {
            'Голова': ['Head', 'Neck', 'Skull', 'Jaw', 'Eye', 'Lips', 'Tongue', 'Teeth'],
            'Тело': ['Spine', 'Chest', 'Torso', 'Hips', 'Pelvis', 'Waist', 'Back'],
            'Левая рука': ['L_Shoulder', 'L_Arm', 'L_Elbow', 'L_Forearm', 'L_Wrist', 'L_Hand', 'L_Finger', 'L_Thumb'],
            'Правая рука': ['R_Shoulder', 'R_Arm', 'R_Elbow', 'R_Forearm', 'R_Wrist', 'R_Hand', 'R_Finger', 'R_Thumb'],
            'Левая нога': ['L_Hip', 'L_Leg', 'L_Knee', 'L_Shin', 'L_Foot', 'L_Toe', 'L_Ankle'],
            'Правая нога': ['R_Hip', 'R_Leg', 'R_Knee', 'R_Shin', 'R_Foot', 'R_Toe', 'R_Ankle'],
          };

          const boneNames = [];
          head.armature.traverse((obj) => {
            if (obj.isBone) boneNames.push(obj.name);
          });

          for (const [cat, patterns] of Object.entries(categories)) {
            const matched = boneNames.filter(b => patterns.some(p => b.toLowerCase().includes(p.toLowerCase())));
            if (matched.length) {
              list.append("div").attr("class", "bonecontrol-category").text(cat);
              matched.forEach(b => {
                list.append("div")
                  .attr("class", "bonecontrol-item" + (b === _boneName ? " selected" : ""))
                  .attr("data-bone", b).text(b);
              });
            }
          }

          const rest = boneNames.filter(b => !Object.values(categories).flat().some(p => b.toLowerCase().includes(p.toLowerCase())));
          if (rest.length) {
            list.append("div").attr("class", "bonecontrol-category").text("Другие");
            rest.forEach(b => {
              list.append("div")
                .attr("class", "bonecontrol-item" + (b === _boneName ? " selected" : ""))
                .attr("data-bone", b).text(b);
            });
          }

          console.log('Bones populated, count:', boneNames.length);
        } else {
          console.log('head or head.armature not ready yet');
        }
      }, 100);
    }
  };

  d3.select("#bonecontroltoggle").on("click", function(ev) {
    window.toggleBonePanel();
  });

  d3.select("#bonecontrol-list").on("click", function(ev) {
    const target = d3.select(ev.target);
    if (target.classed("bonecontrol-item")) {
      selectBone(target.attr("data-bone"));
    }
  });

  d3.select("#bonecontrol-search").on("input", function() {
    const q = d3.select(this).property("value").toLowerCase().trim();
    d3.selectAll("#bonecontrol-list > .bonecontrol-item").each(function() {
      const item = d3.select(this);
      const match = !q || item.text().toLowerCase().includes(q);
      item.classed("hidden", !match);
    });
    d3.selectAll("#bonecontrol-list > .bonecontrol-category").each(function() {
      const cat = d3.select(this);
      const next = cat.node().nextElementSibling;
      let hasVisible = false;
      let el = next;
      while (el && !el.classList.contains("bonecontrol-category")) {
        if (el.classList.contains("bonecontrol-item") && !el.classList.contains("hidden")) {
          hasVisible = true;
          break;
        }
        el = el.nextElementSibling;
      }
      cat.classed("hidden", !hasVisible);
    });
  });

  // ========== TOGGLE MODEL VISIBILITY ==========
  var modelHidden = localStorage.getItem('model-hidden') === 'true';
  var btnToggleModel = document.getElementById('btn-toggle-model');
  var mainEl = document.getElementById('main');
  if (modelHidden && mainEl) {
    mainEl.style.display = 'none';
    if (btnToggleModel) {
      btnToggleModel.classList.add('active');
      btnToggleModel.title = 'Show Model';
      btnToggleModel.innerHTML = '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.43-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78l3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z" fill="currentColor"/></svg>';
    }
  }
  if (btnToggleModel) {
    btnToggleModel.addEventListener('click', function() {
      if (!mainEl) return;
      var isHidden = mainEl.style.display === 'none';
      mainEl.style.display = isHidden ? '' : 'none';
      this.classList.toggle('active', !isHidden);
      localStorage.setItem('model-hidden', !isHidden);
      this.title = isHidden ? 'Hide Model' : 'Show Model';
      if (!isHidden) {
        this.innerHTML = '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.43-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78l3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z" fill="currentColor"/></svg>';
      } else {
        this.innerHTML = '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z" fill="currentColor"/></svg>';
      }
    });
  }

  // ========== DRAGGABLE AVATAR ==========
  (function() {
    var avatarMain = document.getElementById('main');
    var dragHandle = document.getElementById('avatar-drag-handle');
    var resetBtn = document.getElementById('avatar-reset-pos');
    var storageKey = 'avatar-position';
    var defaultRight = 20;
    var defaultBottom = 48;

    if (!avatarMain || !dragHandle) return;

    /* Позиция хранится в координатах окна, где карточку перетащили. В другом
       окне (окно-оверлей аватара 340×480) те же left/top уносят её за экран —
       аватар выглядел пустым. Поэтому восстановленную позицию вписываем в
       текущий вьюпорт, как это делает само перетаскивание. */
    function clampIntoView() {
      var rect = avatarMain.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      var maxLeft = Math.max(0, window.innerWidth - rect.width);
      var maxTop = Math.max(0, window.innerHeight - rect.height);
      var left = Math.min(Math.max(0, rect.left), maxLeft);
      var top = Math.min(Math.max(0, rect.top), maxTop);
      if (left !== rect.left || top !== rect.top) {
        avatarMain.style.left = left + 'px';
        avatarMain.style.top = top + 'px';
        avatarMain.style.right = 'auto';
        avatarMain.style.bottom = 'auto';
      }
    }

    function restorePosition() {
      var saved = localStorage.getItem(storageKey);
      if (saved) {
        try {
          var pos = JSON.parse(saved);
          avatarMain.style.left = pos.left;
          avatarMain.style.top = pos.top;
          avatarMain.style.right = 'auto';
          avatarMain.style.bottom = 'auto';
          clampIntoView();
        } catch(e) {
          resetPosition();
        }
      } else {
        resetPosition();
      }
    }

    function resetPosition() {
      avatarMain.style.left = 'auto';
      avatarMain.style.top = 'auto';
      avatarMain.style.right = defaultRight + 'px';
      avatarMain.style.bottom = defaultBottom + 'px';
      localStorage.removeItem(storageKey);
    }

    function savePosition() {
      var rect = avatarMain.getBoundingClientRect();
      localStorage.setItem(storageKey, JSON.stringify({
        left: rect.left + 'px',
        top: rect.top + 'px'
      }));
    }

    var isDragging = false;
    var startX, startY, startLeft, startTop;

    function onMouseMove(e) {
      if (!isDragging) return;
      var dx = e.clientX - startX;
      var dy = e.clientY - startY;
      var newLeft = startLeft + dx;
      var newTop = startTop + dy;
      var rect = avatarMain.getBoundingClientRect();
      newLeft = Math.max(0, Math.min(newLeft, window.innerWidth - rect.width));
      newTop = Math.max(0, Math.min(newTop, window.innerHeight - rect.height));
      avatarMain.style.left = newLeft + 'px';
      avatarMain.style.top = newTop + 'px';
      avatarMain.style.right = 'auto';
      avatarMain.style.bottom = 'auto';
    }

    function onMouseUp() {
      if (!isDragging) return;
      isDragging = false;
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      savePosition();
    }

    dragHandle.addEventListener('mousedown', function(e) {
      e.preventDefault();
      e.stopPropagation();
      var rect = avatarMain.getBoundingClientRect();
      isDragging = true;
      startX = e.clientX;
      startY = e.clientY;
      startLeft = rect.left;
      startTop = rect.top;
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });

    // Hover border: since #main has pointer-events:none, use dragHandle events
    dragHandle.addEventListener('mouseenter', function() {
      avatarMain.classList.add('hover-border');
    });
    dragHandle.addEventListener('mouseleave', function() {
      avatarMain.classList.remove('hover-border');
    });

    if (resetBtn) {
      resetBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        resetPosition();
      });
    }

    // ── Avatar resize handles ──
    // ── Size input button ──
    var sizeStorageKey = 'avatar-size';
    function restoreSize() {
      var saved = localStorage.getItem(sizeStorageKey);
      if (saved) {
        try { var sz = JSON.parse(saved); avatarMain.style.width = sz.w; avatarMain.style.height = sz.h; } catch(e) {}
      }
    }
    function applySize(w, h) {
      avatarMain.style.width = w + 'px';
      avatarMain.style.height = h + 'px';
      localStorage.setItem(sizeStorageKey, JSON.stringify({w: w + 'px', h: h + 'px'}));
    }
    window.hideAvatarSizeModal = function() {
      document.getElementById('avatar-size-modal').classList.add('hidden');
    };
    window.applyAvatarSize = function() {
      var w = parseInt(document.getElementById('avatar-size-w').value);
      var h = parseInt(document.getElementById('avatar-size-h').value);
      if (isNaN(w) || isNaN(h) || w < 50 || h < 50 || w > 2000 || h > 2000) {
        alert('Минимум 50×50, максимум 2000×2000');
        return;
      }
      applySize(w, h);
      window.hideAvatarSizeModal();
    };
    function promptSize() {
      var curW = parseInt(avatarMain.style.width) || 300;
      var curH = parseInt(avatarMain.style.height) || 500;
      document.getElementById('avatar-size-w').value = curW;
      document.getElementById('avatar-size-h').value = curH;
      document.getElementById('avatar-size-modal').classList.remove('hidden');
    }
    // Add size button next to reset position
    if (resetBtn && resetBtn.parentNode) {
      var sizeBtn = document.createElement('div');
      sizeBtn.id = 'avatar-size-btn';
      sizeBtn.className = 'avatar-reset-pos';
      sizeBtn.title = 'Изменить размер';
      sizeBtn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M21 3H3v8h2V5h6V3h10zm0 18v-8h-2v6h-6v2h8zM3 21h8v-2H5v-6H3v8z" fill="currentColor"/></svg>';
      resetBtn.parentNode.insertBefore(sizeBtn, resetBtn.nextSibling);
      sizeBtn.addEventListener('click', function(e) { e.stopPropagation(); promptSize(); });
    }

    restorePosition();
    restoreSize();
    /* Окно-оверлей меняет размер (пресеты s/m/l) — сохранённая позиция
       карточки не должна оставаться за новыми границами. */
    window.addEventListener('resize', function() { clampIntoView(); });
  })();

  function applyBoneTransform() {
    if (!_boneName || !head || !head.armature) return;
    let bone = null;
    head.armature.traverse((obj) => {
      if (obj.isBone && obj.name === _boneName) bone = obj;
    });
    if (!bone) return;
    bone.rotation.x = THREE.MathUtils.degToRad(parseFloat(d3.select("#bonecontrolrotx").property("value")));
    bone.rotation.y = THREE.MathUtils.degToRad(parseFloat(d3.select("#bonecontrolroty").property("value")));
    bone.rotation.z = THREE.MathUtils.degToRad(parseFloat(d3.select("#bonecontrolrotz").property("value")));
    bone.position.x = parseFloat(d3.select("#bonecontrolposx").property("value"));
    bone.position.y = parseFloat(d3.select("#bonecontrolposy").property("value"));
    bone.position.z = parseFloat(d3.select("#bonecontrolposz").property("value"));
    bone.scale.x = parseFloat(d3.select("#bonecontrolscalex").property("value"));
    bone.scale.y = parseFloat(d3.select("#bonecontrolscaley").property("value"));
    bone.scale.z = parseFloat(d3.select("#bonecontrolscalez").property("value"));
    bone.updateMatrixWorld(true);
  }

  d3.selectAll("#bonecontrolrotx,#bonecontrolroty,#bonecontrolrotz").on("input change", function() {
    const id = d3.select(this).attr("id");
    d3.select("#" + id + "val").text(Math.round(parseFloat(d3.select(this).property("value"))) + "°");
    applyBoneTransform();
  });
  d3.selectAll("#bonecontrolposx,#bonecontrolposy,#bonecontrolposz").on("input change", function() {
    const id = d3.select(this).attr("id");
    d3.select("#" + id + "val").text(parseFloat(d3.select(this).property("value")).toFixed(2));
    applyBoneTransform();
  });
  d3.selectAll("#bonecontrolscalex,#bonecontrolscaley,#bonecontrolscalez").on("input change", function() {
    const id = d3.select(this).attr("id");
    d3.select("#" + id + "val").text(parseFloat(d3.select(this).property("value")).toFixed(2));
    applyBoneTransform();
  });

  d3.select("#bonecontrolreset").on("click", function() {
    if (!_boneName || !head || !head.armature) return;
    let bone = null;
    head.armature.traverse((obj) => {
      if (obj.isBone && obj.name === _boneName) bone = obj;
    });
    if (!bone) return;
    bone.rotation.set(0, 0, 0);
    bone.position.set(0, 0, 0);
    bone.scale.set(1, 1, 1);
    bone.updateMatrixWorld(true);
    d3.select("#bonecontrolrotx").property("value", 0);
    d3.select("#bonecontrolroty").property("value", 0);
    d3.select("#bonecontrolrotz").property("value", 0);
    d3.select("#bonecontrolrotxval").text("0°");
    d3.select("#bonecontrolrotyval").text("0°");
    d3.select("#bonecontrolrotzval").text("0°");
    d3.select("#bonecontrolposx").property("value", 0);
    d3.select("#bonecontrolposy").property("value", 0);
    d3.select("#bonecontrolposz").property("value", 0);
    d3.select("#bonecontrolposxval").text("0");
    d3.select("#bonecontrolposyval").text("0");
    d3.select("#bonecontrolposzval").text("0");
    d3.select("#bonecontrolscalex").property("value", 1);
    d3.select("#bonecontrolscaley").property("value", 1);
    d3.select("#bonecontrolscalez").property("value", 1);
    d3.select("#bonecontrolscalexval").text("1");
    d3.select("#bonecontrolscaleyval").text("1");
    d3.select("#bonecontrolscalezval").text("1");
  });

  d3.select("#bonecontrolresetall").on("click", function() {
    if (!head || !head.armature) return;
    head.armature.traverse((obj) => {
      if (obj.isBone) {
        obj.rotation.set(0, 0, 0);
        obj.position.set(0, 0, 0);
        obj.scale.set(1, 1, 1);
        obj.updateMatrixWorld(true);
      }
    });
    d3.select("#bonecontrolreset").dispatch("click");
  });

  // ── Save Bone Animation ──
  window.showSaveBoneAnim = function() {
    document.getElementById('save-bone-anim-modal').classList.remove('hidden');
  };
  window.hideSaveBoneAnim = function() {
    document.getElementById('save-bone-anim-modal').classList.add('hidden');
  };
  // ── Bone Recording (keyframe timeline) ──
  let _boneRecording = false;
  let _boneRecordTimer = null;
  let _boneRecordStart = 0;
  let _boneRecordFrames = [];   // [{t_ms, bones: {name: {rot:[rx,ry,rz], pos:[x,y,z], scale:[x,y,z]}}}]
  let _lastBoneAnim = null;     // последняя сохранённая/записанная анимация для проигрывания
  const _boneFPS = () => parseInt(cfg('bonecontrol-fps')) || 30;
  const _boneFrameMs = () => 1000 / _boneFPS();

  function _captureAllBones() {
    if (!head || !head.armature) return {};
    const out = {};
    head.armature.traverse((obj) => {
      if (obj.isBone && obj.name) {
        out[obj.name] = {
          rot: [THREE.MathUtils.radToDeg(obj.rotation.x),
                THREE.MathUtils.radToDeg(obj.rotation.y),
                THREE.MathUtils.radToDeg(obj.rotation.z)],
          pos: [obj.position.x, obj.position.y, obj.position.z],
          scale: [obj.scale.x, obj.scale.y, obj.scale.z],
        };
      }
    });
    return out;
  }

  window.toggleBoneRecord = function() {
    const btn = document.getElementById('bonecontrolrecord');
    if (!_boneRecording) {
      // Старт
      _boneRecording = true;
      _boneRecordFrames = [];
      _boneRecordStart = performance.now();
      btn.textContent = '■ Стоп';
      btn.classList.add('selected');
      const tick = () => {
        if (!_boneRecording) return;
        const t = performance.now() - _boneRecordStart;
        const bones = _captureAllBones();
        _boneRecordFrames.push({ t_ms: Math.round(t), bones });
        document.getElementById('bonecontrolframes').textContent = _boneRecordFrames.length + ' кадров';
        _boneRecordTimer = setTimeout(tick, _boneFrameMs());
      };
      tick();
    } else {
      // Стоп
      _boneRecording = false;
      if (_boneRecordTimer) { clearTimeout(_boneRecordTimer); _boneRecordTimer = null; }
      btn.textContent = '⏺ Запись';
      btn.classList.remove('selected');
      if (_boneRecordFrames.length === 0) { alert('Нет кадров для сохранения'); return; }
      // Предложить сохранить
      showSaveBoneAnim();
    }
  };

  // Преобразовать кадры в MotionEngine motion (bone tracks)
  function _framesToMotion(name, frames, fps) {
    if (!frames || !frames.length) return null;
    const dt = [];
    const bonesTracks = {};   // bone -> [[rx,ry,rz], ...]
    // Соберём все уникальные имена костей из всех кадров
    const boneNames = new Set();
    for (const f of frames) {
      for (const bn of Object.keys(f.bones || {})) boneNames.add(bn);
    }
    // dt в мс — каждый кадр
    for (let i = 0; i < frames.length; i++) {
      dt.push(frames[i].t_ms);
    }
    for (const bn of boneNames) {
      const amp = [];
      for (const f of frames) {
        const b = f.bones[bn];
        if (b) amp.push([b.rot[0] / 180, b.rot[1] / 180, b.rot[2] / 180]);
        else amp.push([0, 0, 0]);
      }
      // MotionEngine: _overlay.bones[b].amp — массив кадров
      bonesTracks[bn] = { freq: 0, amp, phase: 0 };
    }
    return {
      _description: `Recorded bone animation: ${name} (${frames.length} frames @ ${fps} fps)`,
      _tags: ["bone", "recorded", name],
      _track: "action",
      dt,
      vs: {},
      _overlay: {
        bones: bonesTracks,
        duration: frames[frames.length - 1].t_ms || 1000
      }
    };
  }

  window.playLastBoneAnim = async function() {
    if (!_lastBoneAnim) {
      // Попробуем загрузить с сервера последнюю
      try {
        const r = await fetch('/api/avatar/motions');
        const j = await r.json();
        const motions = j.motions || {};
        const names = Object.keys(motions);
        if (names.length === 0) { alert('Нет сохранённых анимаций'); return; }
        const last = names[names.length - 1];
        _lastBoneAnim = last;
      } catch(e) { alert('Нет последней анимации'); return; }
    }
    if (window.motionEngine && typeof window.motionEngine.play === 'function') {
      try { await window.motionEngine.play(_lastBoneAnim); }
      catch(e) { console.error('[BONE] play error', e); alert('Ошибка воспроизведения: ' + e.message); }
    } else if (window.playMotion) {
      try { await window.playMotion(_lastBoneAnim); }
      catch(e) { console.error('[BONE] play error', e); }
    }
  };

  window.saveBoneAnim = async function() {
    const name = document.getElementById('save-bone-anim-name').value.trim();
    if (!name) { alert('Введите название анимации'); return; }
    if (!head || !head.armature) { alert('Аватар не загружен'); return; }

    let motionData;
    let isMulti = false;
    if (_boneRecordFrames && _boneRecordFrames.length > 1) {
      motionData = _framesToMotion(name, _boneRecordFrames, _boneFPS());
      isMulti = true;
    } else if (_boneName) {
      // Статичная поза одной кости
      let bone = null;
      head.armature.traverse((obj) => { if (obj.isBone && obj.name === _boneName) bone = obj; });
      if (!bone) { alert('Кость не найдена'); return; }
      const rx = THREE.MathUtils.radToDeg(bone.rotation.x);
      const ry = THREE.MathUtils.radToDeg(bone.rotation.y);
      const rz = THREE.MathUtils.radToDeg(bone.rotation.z);
      motionData = {
        _description: `Bone pose: ${_boneName}`,
        _tags: ["bone", _boneName, "pose"],
        _track: "action",
        dt: [1000],
        vs: {},
        _overlay: {
          bones: {
            [_boneName]: { freq: 0, amp: [[rx/180, ry/180, rz/180]], phase: 0 }
          },
          duration: 1000
        }
      };
    } else {
      alert('Выберите кость или запишите кадры'); return;
    }
    try {
      const resp = await fetch('/api/avatar/motion/create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, motion: motionData, description: motionData._description, tags: motionData._tags})
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        _lastBoneAnim = name;
        // Reload custom motions
        const rmResp = await fetch('/api/avatar/motions');
        if (rmResp.ok) {
          const customMotions = await rmResp.json();
          const count = Object.keys(customMotions).length;
          if (count > 0 && window.motionEngine) {
            window.motionEngine.registerMotions(customMotions);
            console.log('[BONE] registered', count, 'custom motions');
          }
        }
        // Очистим записанные кадры
        if (isMulti) {
          _boneRecordFrames = [];
          const fc = document.getElementById('bonecontrolframes');
          if (fc) fc.textContent = '0 кадров';
        }
        alert(`Анимация "${name}" сохранена (${isMulti ? motionData.dt.length + ' кадров' : 'поза'})`);
        hideSaveBoneAnim();
      } else {
        alert('Ошибка: ' + (data.error || 'Неизвестная ошибка'));
      }
    } catch(e) {
      console.error('[BONE] save error:', e);
      alert('Ошибка сети: ' + e.message);
    }
  };

})();

// Talking Head
let head;
let morphTimer = null;

function headLoaded(firsttime=false) {

  // Populate emoji tester
  if ( firsttime ) {
    const elEmojis = d3.select("#emojis");
    for( let [em,x] of Object.entries(head.animEmojis) ) {
      elEmojis.append("div")
        .classed("emoji", true)
        .on('click', function(ev) {
          const e = d3.select(this);
          const name = e.text();

          // Clear timeouts
          if ( morphTimer ) {
            clearTimeout(morphTimer);
            morphTimer = null;
          }

          // Clear morphs
          head.getMorphTargetNames().forEach( mt => {
            const el = d3.select('#' + CSS.escape(mt));
            if ( !el.empty() ) {
              let v = testerGetValue(mt,null);
              if ( parseFloat(el.property("value")) !== v ) {
                el.property("value",v);
                d3.select(el.node().nextElementSibling).html(testerGetLabel(mt,v));
                head.setFixedValue(mt,testerGetFixedValue(mt,v));
              }
            }
          });

          const selected = e.classed("selected");
          d3.selectAll(".emoji").classed("selected",false);
          if ( selected ) {
            d3.selectAll(".emoji").classed("selected",false);
            testerUpdateMorphData();
          } else {
            e.classed("selected",true);

            // Animate
            head.playGesture(name,3);

            morphTimer = setTimeout( () => {
              morphTimer = null;
              const o = head.animEmojis[name];
              if ( o ) {
                head.getMorphTargetNames().forEach( mt => {
                  let val = null;
                  if ( o.vs &&  o.vs[mt] ) {
                    val = o.vs[mt][o.vs[mt].length-1];
                    if ( Array.isArray(val) ) val = val[val.length-1];
                  }
                  const el = d3.select('#' + CSS.escape(mt));
                  if ( !el.empty() ) {
                    let v = testerGetValue(mt,val);
                    if ( parseFloat(el.property("value")) !== v ) {
                      el.property("value",v);
                      d3.select(el.node().nextElementSibling).html(testerGetLabel(mt,v));
                      head.setFixedValue(mt,testerGetFixedValue(mt,v));
                    }
                  }
                });
                testerUpdateMorphData();
              }
            }, 3500);
          }
        })
        .html(em);
    }
  }

  // Populate morph targets (a.k.a. blend shapes)
  const elMorphs = d3.select("#morphs");
  elMorphs.html("");
  head.getMorphTargetNames().forEach( mt => {
    let v = head.getFixedValue(mt);

    const morph = elMorphs.append("div")
      .classed("row", true)
      .classed("morph", true);

    morph.append("div")
      .classed("text", true)
      .classed("label", true)
      .html(mt);

    morph.append("input")
      .property("id", mt)
      .property("type", "range")
      .property("min", testerGetMin(mt))
      .property("max", testerGetMax(mt))
      .property("step", testerGetStep(mt))
      .property("value", testerGetValue(mt,v))
      .on('input change keyup', function(ev) {
        const e = d3.select(this);
        const mt = e.property("id");
        let v = parseFloat(e.property("value"));
        d3.select(e.node().nextElementSibling).html(testerGetLabel(mt,v));
        head.setFixedValue(mt,testerGetFixedValue(mt,v));
        testerUpdateMorphData();
      });

    morph.append("div")
      .classed("text", true)
      .html(testerGetLabel(mt,v));
  });

  // Populate skinned meshes
  const elSkinnedMeshes = d3.select("#skinnedmeshes");
  elSkinnedMeshes.html("");
  head.armature.traverse( x => {
    if ( x.isSkinnedMesh ) {
      elSkinnedMeshes.append("div")
        .classed("command", true)
        .classed("selected", x.visible )
        .on('click', function(ev) {
          const e = d3.select(this);
          const sel = e.classed('selected');
          e.classed('selected', !sel)
          x.visible = !sel;
        })
        .html(x.name);
    }
  });

  // Populate dynamic bones
  const elBonesHelpers = d3.select("#boneshelpers");
  const elBonesSelect = d3.select("#bonesselect");
  elBonesHelpers.html("");
  elBonesSelect.html("");
  const names = head.dynamicbones?.getBoneNames();
  if ( names && names.length ) {

    // Director
    d3.select("#bonesfeaturedynamic")
      .on("click", null)
      .on("click", function(ev) {
        const e = d3.select(this);
        const sel = !e.classed('selected');
        e.classed('selected', sel);
        if ( sel === true ) {
          head.dynamicbones.start();
        } else {
          head.dynamicbones.stop();
        }
      })
      .classed("selected", true)
      .classed("disabled", false);
    d3.select("#bonesfeatureexcludes")
      .on("click", null)
      .on("click", function(ev) {
        const e = d3.select(this);
        const sel = !e.classed('selected');
        e.classed('selected', sel);
        head.dynamicbones.setOptionValue("isExcludes",sel);
      })
      .classed("selected", true)
      .classed("disabled", false);
    d3.select("#bonesfeaturepivots")
      .on("click", null)
      .on("click", function(ev) {
        const e = d3.select(this);
        const sel = !e.classed('selected');
        e.classed('selected', sel);
        head.dynamicbones.setOptionValue("isPivots",sel);
      })
      .classed("selected", true)
      .classed("disabled", false);
    d3.select("#bonesfeaturelimits")
      .on("click", null)
      .on("click", function(ev) {
        const e = d3.select(this);
        const sel = !e.classed('selected');
        e.classed('selected', sel);
        head.dynamicbones.setOptionValue("isLimits",sel);
      })
      .classed("selected", true)
      .classed("disabled", false);

    // Bone testers
    d3.selectAll(".bonetester").on("click", function(ev) {
      const e = d3.select(this);
      const name = d3.select(".dynamicbone.selected").text();
      const data = head?.dynamicbones?.data?.find( x => x.name === name );
      if ( data ) {
        let dir = 0;
        switch( e.text() ) {
          case 'x': dir = 0; break;
          case 'y': dir = 1; break;
          case 'z': dir = 2; break;
          case 't': dir = 3;
        }
        data.v[dir] = 100;
      }
    });

    // Show all
    elBonesHelpers.append("div")
      .property("id","bonehelpershowall")
      .classed("command", true)
      .on('click', function(ev) {
        const e = d3.select(this);
        const sel = e.classed('selected');
        e.classed('selected', !sel)
        if ( sel ) {
          head.dynamicbones.showHelpers(false);
        } else {
          head.dynamicbones.showHelpers(true);
        }
      })
      .html("ALL");

    names.forEach( (name,i) => {

      elBonesHelpers.append("div")
        .classed("command", true)
        .classed("bonehelper", true)
        .classed("selected", head.dynamicbones.getValue(name,"helper") || false )
        .on('click', function(ev) {
          const e = d3.select(this);
          const sel = e.classed('selected');
          e.classed('selected', !sel)
          head.dynamicbones.setValue(name,"helper",!sel);
        })
        .html(name);

      elBonesSelect.append("div")
        .classed("command", true)
        .classed("dynamicbone", true)
        .classed("selected", i === 0)
        .on('click', function(ev) {
          const e = d3.select(this);
          d3.selectAll(".dynamicbone").classed('selected',false);
          e.classed('selected',true);

          const type = head.dynamicbones.getValue(name,"type");
          d3.selectAll(".bonetype").classed('selected',false);
          d3.select("#bonetype"+type).classed('selected',true);

          let k = head.dynamicbones.getValue(name,"stiffness");
          d3.select("#stiffnesslock").classed("selected", !Number.isNaN(k) );
          if ( !Number.isNaN(k) ) {
            k = Array(4).fill(k);
          }
          d3.select('#stiffnessx').property('value', k[0] ).node().nextElementSibling.textContent = k[0];
          d3.select('#stiffnessy').property('value', k[1] ).node().nextElementSibling.textContent = k[1];
          d3.select('#stiffnessz').property('value', k[2] ).node().nextElementSibling.textContent = k[2];
          d3.select('#stiffnesst').property('value', k[3] ).node().nextElementSibling.textContent = k[3];

          let c = head.dynamicbones.getValue(name,"damping");
          d3.select("#dampinglock").classed("selected", !Number.isNaN(c) );
          if ( !Number.isNaN(k) ) {
            c = Array(4).fill(c);
          }
          d3.select('#dampingx').property('value', c[0] ).node().nextElementSibling.textContent = c[0];
          d3.select('#dampingy').property('value', c[1] ).node().nextElementSibling.textContent = c[1];
          d3.select('#dampingz').property('value', c[2] ).node().nextElementSibling.textContent = c[2];
          d3.select('#dampingt').property('value', c[3] ).node().nextElementSibling.textContent = c[3];

          const ext = head.dynamicbones.getValue(name,"external") || 1.0;
          d3.select('#external').property('value', ext ).node().nextElementSibling.textContent = ext;

          let l = head.dynamicbones.getValue(name,"limits");
          if ( l ) {
            if ( !l[0] ) l[0] = [null,null];
            if ( !l[1] ) l[1] = [null,null];
            if ( !l[2] ) l[2] = [null,null];
            if ( !l[3] ) l[3] = [null,null];
          } else {
            l = [[null,null],[null,null],[null,null],[null,null]];
          }
          l = l.flat();
          [ '#limitsx1','#limitsx2','#limitsy1','#limitsy2','#limitsz1','#limitsz2','#limitst1','#limitst2' ].forEach( (x,i) => {
            d3.select(x+'null').classed("selected", l[i] !== null );
            d3.select(x)
              .property("value",l[i])
              .classed("disabled", l[i] === null )
              .node().nextElementSibling.textContent = l[i] === null ? "" : (l[i] + ' m');
          });

          const deltal = head.dynamicbones.getValue(name,"deltaLocal") || [0,0,0];
          d3.select('#deltalx').property('value', deltal[0] ).node().nextElementSibling.textContent = deltal[0];
          d3.select('#deltaly').property('value', deltal[1] ).node().nextElementSibling.textContent = deltal[1];
          d3.select('#deltalz').property('value', deltal[2] ).node().nextElementSibling.textContent = deltal[2];

          const deltaw = head.dynamicbones.getValue(name,"deltaWorld") || [0,0,0];
          d3.select('#deltawx').property('value', deltaw[0] ).node().nextElementSibling.textContent = deltaw[0];
          d3.select('#deltawy').property('value', deltaw[1] ).node().nextElementSibling.textContent = deltaw[1];
          d3.select('#deltawz').property('value', deltaw[2] ).node().nextElementSibling.textContent = deltaw[2];

          const pivot = head.dynamicbones.getValue(name,"pivot") || false;
          d3.select("#pivot").classed("selected",pivot);

        })
        .html(name);
    });

    d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );

    // Change type
    d3.selectAll(".bonetype")
      .classed("disabled", false)
      .on('click', null)
      .on('click', function(ev) {
        const e = d3.select(this);
        d3.selectAll(".bonetype").classed('selected',false);
        e.classed('selected',true);
        const name = d3.select(".dynamicbone.selected").text();
        const type = e.text();
        head.dynamicbones.setValue(name,"type",type);
        d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );
      });

    // Change stiffness
    d3.selectAll(".stiffness")
      .classed("disabled", false)
      .on('input.update change.update keyup.update', null)
      .on('input.update change.update keyup.update', function(ev) {
        const e = d3.select(this);
        const name = d3.select(".dynamicbone.selected").text();
        const lock = d3.select("#stiffnesslock").classed("selected");
        if ( lock ) {
          d3.selectAll(".stiffness").property('value',e.property('value')).nodes().forEach( x => {
            x.nextElementSibling.textContent = e.property('value');
          });
        }
        head.dynamicbones.setValue(name,"stiffness",[
          parseFloat( d3.select('#stiffnessx').property('value') ),
          parseFloat( d3.select('#stiffnessy').property('value') ),
          parseFloat( d3.select('#stiffnessz').property('value') ),
          parseFloat( d3.select('#stiffnesst').property('value') )
        ]);
        d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );
      });

    // Change damping
    d3.selectAll(".damping")
      .classed("disabled", false)
      .on('input.update change.update keyup.update', null)
      .on('input.update change.update keyup.update', function(ev) {
        const e = d3.select(this);
        const name = d3.select(".dynamicbone.selected").text();
        const lock = d3.select("#dampinglock").classed("selected");
        if ( lock ) {
          d3.selectAll(".damping").property('value',e.property('value')).nodes().forEach( x => {
            x.nextElementSibling.textContent = e.property('value');
          });
        }
        head.dynamicbones.setValue(name,"damping",[
          parseFloat( d3.select('#dampingx').property('value') ),
          parseFloat( d3.select('#dampingy').property('value') ),
          parseFloat( d3.select('#dampingz').property('value') ),
          parseFloat( d3.select('#dampingt').property('value') )
        ]);
        d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );
      });

    // Locks
    d3.selectAll("#stiffnesslock,#dampinglock")
      .classed("disabled", false)
      .on("click",null)
      .on('click', function(ev) {
        const e = d3.select(this);
        const sel = e.classed('selected');
        e.classed('selected', !sel);
        d3.select(this.parentNode).select("input").dispatch("change");
      });

    // Change external
    d3.select("#external")
      .classed("disabled", false)
      .on('input.update change.update keyup.update', null)
      .on('input.update change.update keyup.update', function(ev) {
        const name = d3.select(".dynamicbone.selected").text();
        head.dynamicbones.setValue(name,"external",
          parseFloat( d3.select('#external').property('value') )
        );
        d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );
      });

    // Change limits
    d3.selectAll(".limits")
      .classed("disabled", false)
      .on('input.update change.update keyup.update', null)
      .on('input.update change.update keyup.update', function(ev) {
        let val = [ '#limitsx1','#limitsx2','#limitsy1','#limitsy2','#limitsz1','#limitsz2','#limitst1','#limitst2' ].map( (x,i) => {
          return d3.select(x+'null').classed("selected" ) ? parseFloat( d3.select(x).property("value") ) : null;
        });
        val = [
          [val[0],val[1]],
          [val[2],val[3]],
          [val[4],val[5]],
          [val[6],val[7]]
        ];
        val = val.map( x => ( x[0] === null && x[1] === null ) ? null : x );
        if ( val.every( x => x === null ) ) val = null;
        const name = d3.select(".dynamicbone.selected").text();
        head.dynamicbones.setValue(name, "limits", val );
        d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );
      });

    // Change limits
    d3.selectAll(".limitsnull")
      .classed("disabled", false)
      .on('click', null)
      .on('click', function(ev) {
        const e = d3.select(this);
        const sel = e.classed('selected');
        e.classed('selected', !sel)
        if ( !sel ) {
          e.node().nextElementSibling.classList.remove('disabled');
        } else {
          e.node().nextElementSibling.classList.add('disabled');
        }
        d3.select(this.parentNode).select("input").dispatch("change");
      });

    // Change delta local
    d3.selectAll(".deltal")
      .classed("disabled", false)
      .on('input.update change.update keyup.update', null)
      .on('input.update change.update keyup.update', function(ev) {
        const name = d3.select(".dynamicbone.selected").text();
        head.dynamicbones.setValue(name,"deltaLocal",[
          parseFloat( d3.select('#deltalx').property('value') ),
          parseFloat( d3.select('#deltaly').property('value') ),
          parseFloat( d3.select('#deltalz').property('value') )
        ]);
        d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );
      });

    // Change delta world
    d3.selectAll(".deltaw")
      .classed("disabled", false)
      .on('input.update change.update keyup.update', null)
      .on('input.update change.update keyup.update', function(ev) {
        const name = d3.select(".dynamicbone.selected").text();
        head.dynamicbones.setValue(name,"deltaWorld",[
          parseFloat( d3.select('#deltawx').property('value') ),
          parseFloat( d3.select('#deltawy').property('value') ),
          parseFloat( d3.select('#deltawz').property('value') )
        ]);
        d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );
      });

    // Change pivot
    d3.select("#pivot")
      .classed("disabled", false)
      .on('click', null)
      .on('click', function(ev) {
        const e = d3.select(this);
        const sel = e.classed('selected');
        e.classed('selected', !sel)
        const name = d3.select(".dynamicbone.selected").text();
        head.dynamicbones.setValue(name,"pivot",!sel);
        d3.select('#bonesdata').property("value", JSON.stringify( head.dynamicbones.getConfig() ) );
      });

    // Select first bone
    d3.select('.dynamicbone').dispatch("click");

  } else {

    d3.selectAll("#bonesfeaturedynamic,#bonesfeatureexcludes,#bonesfeaturepivots,#bonesfeaturelimits,.bonetype,#pivot,#stiffnesslock,#dampinglock")
      .classed("selected", false)
      .classed("disabled", true)
      .on("click", null);

    let row = elBonesHelpers.append("div")
      .classed("row", true)
      .classed("bone", true);

    row.append("div")
      .classed("text", true)
      .classed("label", true)
      .html("N/A");

    row = elBonesSelect.append("div")
      .classed("row", true)
      .classed("bone", true);

    row.append("div")
      .classed("text", true)
      .classed("label", true)
      .html("N/A");

    d3.selectAll(".stiffness,.damping,#external,.limits,.deltal,.deltaw")
      .classed("disabled", true)
      .on('input.update change.update keyup.update', null);

    d3.select('#bonesdata').property("value", "");
  }

  // Unlock Web Audio API
  if ( firsttime ) {
    if ( head.audioCtx.state === 'suspended' ) {
      if ( 'ontouchstart' in window ) {
        let unlockWebAudioAPI = function() {
          head.audioCtx.resume().then( () => {
            document.body.removeEventListener('touchstart', unlockWebAudioAPI);
            document.body.removeEventListener('touchend', unlockWebAudioAPI);
          });
        };
        document.body.addEventListener('touchstart', unlockWebAudioAPI, false);
        document.body.addEventListener('touchend', unlockWebAudioAPI, false);
      }
    }

    setTimeout( () => {
      turnOnEffect();
      reconnectEffect();
      loadConfig();
    }, 1000);
  }
}

// Update progress bar
let progressTimeout;
function progressUpdate(ev) {
  if ( progressTimeout ) {
    clearTimeout(progressTimeout);
    progressTimeout = null;
  } else {
    d3.select("#loading").style("display","block");
  }
  let hideMs = 1000;
  if ( ev.lengthComputable ) {
    let val = Math.min(100,Math.round(ev.loaded/ev.total * 100 ));
    d3.select("#loading-top").style("clip-path", "inset(0 " + (100-val) + "% 0 0)");
    d3.selectAll("#loading-value").text( val + "%" );
    if ( val<100 ) hideMs = 3000;
  } else {
    d3.select("#loading-top").style("clip-path", "inset(0 0 0 0)");
    d3.selectAll("#loading-value").text( "" + ev.loaded );
  }
  progressTimeout = setTimeout( () => {
    d3.select("#loading").style("display","none");
    progressTimeout = null;
  }, hideMs);
}

// Update progress bar using media loading progress
function progressMedia() {
  let bf = this.buffered;
  let dur = this.duration;
  let state = this.readyState;
  if ( bf && bf.length && !Number.isNaN(dur) && dur < Infinity && state > 0 && state < 4) {
    progressUpdate( {
      loaded: bf.end(bf.length-1) - bf.start(bf.length-1),
      total: dur,
      lengthComputable: (dur > 0)
    });
  }
}

function errorShow(error) {
  console.error(error);
}

function testerUpdateMorphData() {
  let s = '{';
  for( let mt of head.getMorphTargetNames() ) {
    const el = d3.select('#' + CSS.escape(mt));
    if ( !el.empty() ) {
      const v = testerGetFixedValue( mt, parseFloat(el.property("value")) );
      if ( v !== null ) {
        s += (s.length > 1 ? ',' : '') + ' ' + mt + ': [' + v + ']';
      }
    }
  }
  s += ' }';
  d3.select('#morphdata').property("value",s).dispatch('change');
}

function testerGetValue(mt,v) {
  if ( mt.startsWith('headRotate') || mt.startsWith('bodyRotate') || mt.startsWith('eyesRotate') ) {
    return ( v === null || v === undefined ) ? 0 : v;
  } else {
    return ( v === null || v === undefined ) ? -0.1 : v;
  }
}

function testerGetFixedValue(mt,v) {
  if ( mt.startsWith('headRotate') || mt.startsWith('bodyRotate') || mt.startsWith('eyesRotate') ) {
    return ( v === 0 ? null : v);
  } else {
    return ( v >= 0 ? v : null);
  }
}

function testerGetMin(mt) {
  if ( mt.startsWith('headRotate') || mt.startsWith('bodyRotate') ) {
    return -0.5;
  } else if ( mt.startsWith('eyesRotate') ) {
    return -1;
  } else {
    return -0.1;
  }
}

function testerGetMax(mt) {
  return (mt.startsWith('headRotate') || mt.startsWith('bodyRotate')) ? 0.5 : 1;
}

function testerGetStep(mt) {
  return ( mt.startsWith('headRotate') || mt.startsWith('bodyRotate') || mt.startsWith('eyesRotate') ) ? 0.05 : 0.1;
}

function testerGetLabel(mt,v) {
  if ( mt.startsWith('headRotate') || mt.startsWith('bodyRotate') || mt.startsWith('eyesRotate') ) {
    return (v ? v : '');
  } else {
    return ( v>=0 ? v : '');
  }
}


// Script
let scriptIsRunning = false;
let scriptBlocks = [];
let scriptIndex = 0;

function scriptStatus(msg=null) {
  const el = d3.select("#scriptstatus");
  if ( msg ) {
    el.text( msg );
  } else if ( scriptIsRunning ) {
    el.text( "ACTIVE" );
  } else {
    const size = scriptBlocks.length;
    if ( size === 0 ) {
      el.text( "" );
    } else if ( scriptIndex === 0 ) {
      el.text( "READY" );
    } else if ( scriptIndex >= size ) {
      el.text( "END" );
    } else {
      el.text( (scriptIndex + 1) + "/" + size );
    }
  }
}

function scriptInit() {
  const script = d3.select("#script").property("value");
  scriptBlocks = script
    .replaceAll('\n\n\n','\n\n').trim()
    .split('\n\n')
    .map( x => x.trim().split('\n')
    .map( y => y.trim() ).filter( y => y.length) )
    .filter( y => y.length );
  scriptIndex = 0;
  scriptStatus();
}

function scriptCommand(ev) {
  if ( ev.altKey ) {
    if ( scriptIsRunning ) {
      if ( ev.keyCode === 37 ) { // Arrow left
        scriptIsRunning = false;
      }
    } else {
      const size = scriptBlocks.length;
      const status = d3.select("#scriptstatus");
      if ( ev.keyCode === 38 ) { // Arrow up
        if ( scriptIndex > 0 ) scriptIndex--;
        scriptStatus();
      } else if ( ev.keyCode === 39 ) { // Arrow right
        if ( scriptIndex < scriptBlocks.length ) {
          scriptIsRunning = true;
          scriptRun(scriptIndex);
          scriptStatus();
        }
      } else if ( ev.keyCode === 40 ) { // Arrow down
        if ( scriptIndex < scriptBlocks.length ) scriptIndex++;
        scriptStatus();
      }
    }
  }
}

window.sleep = ms => new Promise(r => setTimeout(r, ms));
const AsyncFunction = async function () {}.constructor;
async function scriptRun() {
  let node = null;
  for( let i=0; i < scriptBlocks[scriptIndex].length; i++ ) {
    if ( !scriptIsRunning ) break;

    let x = scriptBlocks[scriptIndex][i];

    if ( x.charAt(0) === '#' ) {
      // Comment, ignore
    } else if ( x.charAt(x.length-1) === ';' ) {
      // Function
      try {
        const fn = new AsyncFunction( x );
        await fn();
      } catch( err ) {
        console.error(err);
      }
    } else {

      // First text
      if ( !node ) {
        node = addMessage(false);
      } else {
        addText( node, " " );
      }

      if ( cfg('voice-type') === 'eleven' ) {
        await new Promise(resolve => {
          elevenOnProcessed = resolve;
          elevenSpeak( x + " ", node );
          elevenSpeak( "" );
        });
        elevenOnProcessed = null;
      } else if ( cfg('voice-type') === 'microsoft' ) {
        await new Promise(resolve => {
          microsoftSpeak( x, node, resolve );
        });
      } else {
        await head.speakText( x, {
          lipsyncLang: cfg('voice-lipsync-lang'),
          ttsVoice: cfg('voice-google-id'),
          ttsLang: cfg('voice-google-id').substring(0,5),
          ttsRate: cfg('voice-google-rate'),
          ttsPitch: cfg('voice-google-pitch')
        }, addText.bind(null,node) );
      }

      // Wait for it
      await new Promise(resolve => {
        head.speakMarker(resolve);
      });

    }
  }

  // Close connection
  if ( node ) {
    if ( elevenSocket ) {
      elevenSpeak("",null);
    }
    microsoftSpeak(null);
    d3.selectAll('.blink').classed("blink",false);
  }

  scriptIndex++;
  scriptIsRunning = false;
  scriptStatus();
}






  // ========== SILERO SPEAKER SELECTION ==========
  d3.selectAll("[data-voice-silero-speaker]").on('click.command click.config', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-voice-silero-speaker]").classed('selected', false);
    e.classed('selected', true);
    // Сохраняем в cfg — общий обработчик [data-item] не сработает, потому что
    // .on('click.command', ...) перебивает click.config. Делаем это вручную.
    const item = e.attr('data-item');
    const type = e.attr('data-type');
    if (item && type === 'option') {
      const value = e.attr('data-' + item);
      if (value && !loadingConfig && cfg(item) !== value) {
        cfg(item, value);
        saveConfig();
      }
    }
  });

// Page ready
document.addEventListener('DOMContentLoaded', async function(e) {

  // Add supported UI languages
  const eLanguages = d3.select("#languages");
  for ( const k of Object.keys(i18n) ) {
    eLanguages.append("div")
      .classed("command", true)
      .classed("selected", k === 'ru' )
      .attr("data-item","theme-lang")
      .attr("data-type","option")
      .attr("data-theme-lang",k)
      .attr("data-i18n-text",k);
  }

  // Add avatar links
  const eAvatars = d3.select("#avatars");
  for ( const [i,k] of Object.entries(Object.keys(site.avatars)) ) {
    eAvatars.append("div")
      .classed("command", true)
      .classed("selected", parseInt(i) === 0 )
      .attr("data-item","avatar-name")
      .attr("data-type","option")
      .attr("data-avatar-name", k )
      .attr("data-i18n-site","avatars-"+k);
  }

  // Add Google voices
  const eGoogleVoices = d3.select("#googlevoices");
  for ( const [i,k] of Object.entries(Object.keys(site.googleVoices)) ) {
    let o = site.googleVoices[k];
    eGoogleVoices.append("div")
      .classed("command", true)
      .classed("selected", parseInt(i) === 0 )
      .attr("data-item","voice-google-id")
      .attr("data-type","option")
      .attr("data-voice-google-id",o.id)
      .attr("data-i18n-site","googleVoices-"+k);
  }

  // Add ElevenLabs voices
  const eElevenVoices = d3.select("#elevenvoices");
  for ( const [i,k] of Object.entries(Object.keys(site.elevenVoices)) ) {
    let o = site.elevenVoices[k];
    eElevenVoices.append("div")
      .classed("command", true)
      .classed("selected", parseInt(i) === 0 )
      .attr("data-item","voice-eleven-id")
      .attr("data-type","option")
      .attr("data-voice-eleven-id",o.id)
      .attr("data-i18n-site","elevenVoices-"+k);
  }

  // Add Microsoft voices
  const eMicrosoftVoices = d3.select("#microsoftvoices");
  for ( const [i,k] of Object.entries(Object.keys(site.microsoftVoices)) ) {
    let o = site.microsoftVoices[k];
    eMicrosoftVoices.append("div")
      .classed("command", true)
      .classed("selected", parseInt(i) === 0 )
      .attr("data-item","voice-microsoft-id")
      .attr("data-type","option")
      .attr("data-voice-microsoft-lang",o.lang)
      .attr("data-voice-microsoft-id",o.id)
      .attr("data-voice-microsoft-viseme", o.viseme )
      .attr("data-i18n-site","microsoftVoices-"+k);
  }

  // Add views
  const eViews = d3.select("#views");
  for ( const [k,o] of Object.entries(site.views) ) {
    eViews.append("div")
      .classed("command", true)
      .attr("data-item","view-image")
      .attr("data-type","option")
      .attr("data-image-type",o.type)
      .attr("data-view-image",o.url)
      .attr("data-i18n-site","views-"+k);
  }

  // Add poses
  const ePoses = d3.select("#poses");
  for ( const [k,o] of Object.entries(site.poses) ) {
    ePoses.append("div")
      .classed("command", true)
      .attr("data-pose",o.url)
      .attr("data-i18n-site","poses-"+k);
  }

  // Add gestures
  if ( site.gestures ) {
    const eGestures = d3.select("#gestures");
    for ( const [k,o] of Object.entries(site.gestures) ) {
      eGestures.append("div")
        .classed("command", true)
        .attr("data-gesture",o.name)
        .attr("data-i18n-site","gestures-"+k);
    }
  }

  // Add animations
  const eAnimations = d3.select("#animations");
  for ( const [k,o] of Object.entries(site.animations) ) {
    eAnimations.append("div")
      .classed("command", true)
      .attr("data-animation",o.url)
      .attr("data-i18n-site","animations-"+k);
  }

  // Add impulse responses
  const eImpulses = d3.select("#impulses");
  for ( const [k,o] of Object.entries(site.impulses) ) {
    eImpulses.append("div")
      .classed("command", true)
      .attr("data-item","voice-reverb")
      .attr("data-type","option")
      .attr("data-voice-reverb",o.url)
      .attr("data-i18n-site","impulses-"+k);
  }

  // Add music
  const eMusic = d3.select("#music");
  for ( const [k,o] of Object.entries(site.music) ) {
    eMusic.append("div")
      .classed("command", true)
      .attr("data-item","voice-background")
      .attr("data-type","option")
      .attr("data-voice-background",o.url)
      .attr("data-i18n-site","music-"+k);
  }

  // Translate
  i18nTranslate();

  // 3D-аватар — опция («Аватар» в настройках). Когда он выключен, сервер
  // инжектит в страницу стиль #uiCfgHideAvatar: по его наличию и определяем
  // лёгкий режим. Раньше аватар только скрывался CSS, но three.js, talkinghead,
  // lipsync-модули и GLB всё равно качались и исполнялись — на телефоне
  // main-поток был занят невидимой сценой и экран загрузки зависал навсегда.
  const skip3D = !!document.getElementById('uiCfgHideAvatar');

  if (!skip3D) {

  // Create the talking head avatar
  const nodeAvatar = document.getElementById('avatar');
  const { TalkingHead } = await import('./modules/talkinghead.mjs');
  head = new TalkingHead( nodeAvatar, {
    jwtGet: null,
    ttsEndpoint: (typeof location !== 'undefined' ? location.origin : 'http://localhost:8080') + "/api/tts",
    ttsEngine: "silero",
    cameraZoomEnable: true,
    cameraPanEnable: true,
    cameraView: 'full',
    avatarMood: 'neutral',
    lipsyncModules: ["en", "fi", "lt", "fr", "de", "ru"],
    // Stats display that can be used when testing performance
    statsNode: document.body,
    statsStyle: "position: fixed; bottom: 0px; left: 0px; cursor: pointer; opacity: 0.9; z-index: 10000;"
  });
  window.head = head;

  // ========== MOTION ENGINE ==========
  let motionEngine = null;
  let motionStudio = null;
  try {
    const { MotionEngine } = await import('motion-engine');
    const { MotionStudio } = await import('motion-studio');
    const motions = await import('motion-motions');
    const motionsTH = await import('motion-motions-th');
    motionEngine = new MotionEngine(head);
    motionStudio = new MotionStudio(motionEngine);
    motionEngine.registerMotions(motions.default || motions);
    motionEngine.registerMotions(motionsTH.default || motionsTH);
    // Load custom motions from server
    try {
      const resp = await fetch('/api/avatar/motions');
      if (resp.ok) {
        const customMotions = await resp.json();
        const count = Object.keys(customMotions).length;
        if (count > 0) {
          motionEngine.registerMotions(customMotions);
          console.log('Custom motions registered:', count);
        }
      }
    } catch(e) {
      console.warn('Custom motions not loaded:', e);
    }
    window.motionEngine = motionEngine;
    window.motionStudio = motionStudio;
    console.log('MotionEngine registered', motionEngine.getMotionNames().length, 'motions');
  } catch(e) {
    console.warn('MotionEngine not loaded:', e);
  }

  // Hook oscillation overlay + mood blending into render loop
  if (head && motionEngine) {
    head.opt.update = (dt) => motionEngine.update(dt);
  }

  // Helper: trigger motion by name
  window.playMotion = function(name) {
    if (motionEngine) { motionEngine.play(name); }
  };

  // Helper: trigger list of motions as sequence
  window.playMotions = async function(names) {
    if (!motionEngine) return;
    for (const name of names) {
      try { await motionEngine.play(name); } catch(e) { if (e.name !== 'AbortError') console.warn(e); }
    }
  };

  // Trigger motion based on AI response content
  window.triggerMotionForText = function(text) {
    if (!window.motionEngine || !text) return;
    const lower = text.toLowerCase();
    if (lower.includes('спасибо') || lower.includes('thank')) window.motionEngine.play('thumbup_right');
    else if (lower.includes('привет') || lower.includes('hello') || lower.includes('hi')) window.motionEngine.play('wave_right');
    else if (lower.includes('нет') || lower.includes('no')) window.motionEngine.play('shake_no');
    else if (lower.includes('думаю') || lower.includes('thinking') || lower.includes('maybe')) window.motionEngine.play('thinking');
    else if (lower.includes('удив') || lower.includes('wow') || lower.includes('surprise')) window.motionEngine.play('surprised');
    else if (lower.includes('зл') || lower.includes('angry')) window.motionEngine.play('angry');
    else if (lower.includes('грустн') || lower.includes('sad') || lower.includes('sorry')) window.motionEngine.play('sad');
    else if (lower.includes('смех') || lower.includes('laugh') || lower.includes('haha') || lower.includes('lol')) window.motionEngine.play('laugh');
    else if (lower.includes('приветств') || lower.includes('welcome') || lower.includes('greet')) window.motionEngine.play('happy');
    else window.motionEngine.play('neutral');
  };

  } else {
    console.info('[Infinity] 3D-аватар выключен — three.js, talkinghead, lipsync и GLB не грузятся.');
    window.playMotion = function(){};
    window.playMotions = async function(){};
    window.triggerMotionForText = function(){};
    // headLoaded() в лёгком режиме не вызывается, а панели без неё остались бы
    // прозрачными: показываем их сразу.
    d3.selectAll('.starttransparent').style('opacity','1');
    // head не нужен: конфиг и сессии загружаем как обычно.
    loadConfig();
  }

  // Resume AudioContext on first user gesture (browser autoplay policy)
  let audioCtxResumed = false;
  function resumeAudioCtx() {
    if ( !audioCtxResumed && head && head.audioCtx && head.audioCtx.state === 'suspended' ) {
      head.audioCtx.resume().then(() => { audioCtxResumed = true; }).catch(() => {});
    }
  }
  document.addEventListener('click', resumeAudioCtx, { once: false });
  document.addEventListener('touchstart', resumeAudioCtx, { once: false });
  document.addEventListener('keydown', resumeAudioCtx, { once: false });

  // Follow double clicks
  document.body.addEventListener('dblclick', function(e) {
    if (!head) return;
    e = e || window.event;
    if ( !head.touchAt(e.clientX,e.clientY) ) {
      head.lookAt(e.clientX,e.clientY,500)
    }
  });

  // Avatar-Hermes Gateway polling moved to single loop below (every 1500ms with ?max=8).
  // Legacy 500ms poll removed: it duplicated work, flooded the server,
  // and starved TTS/AI requests of thread-pool slots.

  // Media progress
  d3.select("#video").node().addEventListener("progress", progressMedia);

  // Auto resize textarea
  d3.selectAll("textarea").on("input.resize change.resize keyup.resize paste.resize", function(e) {
    let pos = this.parentElement.parentElement.scrollTop;
    this.style.height = '0px';
    this.style.height = this.scrollHeight+'px';
    this.parentElement.parentElement.scrollTop = pos;
  });

  // Reset moderation flag, score and word count
  d3.selectAll("textarea").on("input.moderate change.moderate paste.moderate", function(e) {
    delete this.dataset.flag;
    delete this.dataset.score;
    delete this.dataset.words;
  });

  // Send message
  d3.select("#input").on("keydown", async function(ev) {
    if ( ev.keyCode === 13 && !ev.shiftKey ) {
      ev.preventDefault();
      const e = d3.select("#input");
      let text = e.property("value");
      if ( head && text.length ) {

        // Template command matching — execute locally and optionally forward to AI
        const templateHandled = await window._checkTemplateMatch(text);
        if (templateHandled) {
          e.property("value", "");
          return;
        }

        if ( cfg('ai-model').startsWith("gpt") ) {
          const m = openaiBuildMessage();
          let flag = await openaiModerateMessage(m);

          const nodeInput = addMessage(true);
          nodeInput.dataset.input = text;
          addText(nodeInput,text);
          e.property("value","");

          if ( flag ) {
            console.info("Moderation flagged.");
            d3.select(nodeInput).classed("grayed", true );
            d3.select(nodeInput .select).classed("selected", true );
            if (window.playMotion) window.playMotion("thinking");
          } else {
            const nodeOutput = addMessage(false);
            sileroReset();
            if (window.playMotion) window.playMotion("turn_around");
            openaiSendMessage(nodeOutput,m);
          }
        } else if ( cfg('ai-model').startsWith("gemini") ) {
          const m = geminiBuildMessage();

          const nodeInput = addMessage(true);
          nodeInput.dataset.input = text;
          addText(nodeInput,text);
          e.property("value","");

          const nodeOutput = addMessage(false);
          sileroReset();
          if (window.playMotion) window.playMotion("turn_around");
          if (window.triggerMotionForText) window.triggerMotionForText(text);
          geminiSendMessage(nodeOutput,m);
        } else if ( cfg('ai-model').startsWith("llama") ) {
          const m = openaiBuildMessage();

          const nodeInput = addMessage(true);
          nodeInput.dataset.input = text;
          addText(nodeInput,text);
          e.property("value","");

          const nodeOutput = addMessage(false);
          sileroReset();
          if (window.playMotion) window.playMotion("turn_around");
          if (window.triggerMotionForText) window.triggerMotionForText(text);
          llamaSendMessage(nodeOutput,m);
        } else if ( cfg('ai-model').startsWith("grok") ) {
          const m = openaiBuildMessage();

          const nodeInput = addMessage(true);
          nodeInput.dataset.input = text;
          addText(nodeInput,text);
          e.property("value","");

          const nodeOutput = addMessage(false);
          sileroReset();
          if (window.playMotion) window.playMotion("turn_around");
          if (window.triggerMotionForText) window.triggerMotionForText(text);
          grokSendMessage(nodeOutput,m);
        } else if ( cfg('ai-model') === "lmstudio" ) {
          const m = openaiBuildMessage();

          const nodeInput = addMessage(true);
          nodeInput.dataset.input = text;
          addText(nodeInput,text);
          e.property("value","");

          const nodeOutput = addMessage(false);
          sileroReset();
          if (window.playMotion) window.playMotion("turn_around");
          if (window.triggerMotionForText) window.triggerMotionForText(text);
          lmstudioSendMessage(nodeOutput,m);
        }

        // Show QA suggestions if available (non-blocking, after AI send)
        setTimeout(() => { if (window.showTemplateSuggestions) window.showTemplateSuggestions(); }, 100);

      }
    }
  });

  // Change UI language
  d3.selectAll("[data-theme-lang]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-theme-lang]").classed('selected',false);
    e.classed('selected',true);
    const lang = e.attr("data-theme-lang");
    cfg('theme-lang',lang);
    i18nTranslate();
    d3.selectAll("[data-range]").dispatch("change");
  });

  // Speak test
  d3.select("#playtest").on("click", function(ev) {
    const e = d3.select("[data-voice-test]");
    let text = e.property("value");
    if ( head && text.length ) {
      if ( cfg('voice-type') === 'eleven' ) {
        elevenSpeak( text + " " );
        elevenSpeak( "" );
      } else if ( cfg('voice-type') === 'microsoft' ) {
        microsoftSpeak( text );
        microsoftSpeak( null );
      } else {
        const exclude = excludesProcess(text);
        head.speakText( text, {
          lipsyncLang: cfg('voice-lipsync-lang'),
          ttsVoice: cfg('voice-google-id'),
          ttsLang: cfg('voice-google-id').substring(0,5),
          ttsRate: cfg('voice-google-rate'),
          ttsPitch: cfg('voice-google-pitch')
        }, null, exclude.excludes );
      }
    }
  });

  // MP3 test file
  d3.select("#loadmp3-file").on("change", function(ev) {
    let file = ev.target.files[0];
    whisperLoadMP3(file);
    ev.target.value = '';
  });

  d3.select("#playmp3").on("click", function(ev) {
    if ( whisperAudio ) {
      head.speakAudio( whisperAudio, { lipsyncLang: whisperLipsyncLang } );
    }
  });

  // Repeat/mirror
  d3.selectAll("#pose-repeat,#animation-repeat,#gesture-repeat,#gesture-mirror").on("click", function(ev) {
    const e = d3.select(this);
    const sel = e.classed("selected");
    e.classed("selected", !sel );
  });

  d3.select("#pause").on("click", function(ev) {
    if (!head) return;
    const e = d3.select(this);
    const sel = e.classed("selected");
    e.classed("selected", !sel );
    if ( !sel ) {
      head.stop();
    } else {
      head.start();
    }
  });

  d3.select("#slowdown").on("click", function(ev) {
    const e = d3.select(this);
    let k = 2 * head.getSlowdownRate();
    let t = i18nWord('Slow-motion') + ' x' + k;
    if ( k >= 16 ) {
      k = 1;
      t = i18nWord('Slow-motion');
    }
    e.classed("selected", (k > 1) ).text(t);
    head.setSlowdownRate( k );
  });

  d3.select("#autorotate").on("click", function(ev) {
    const e = d3.select(this);
    let k = head.getAutoRotateSpeed() + 10;
    let t = i18nWord('Panning') + ' ' + k;
    if ( k >= 60 ) {
      k = 0;
      t = i18nWord('Panning');
    }
    e.classed("selected", (k > 0) ).text(t);
    head.setAutoRotateSpeed( k );
  });

  // Avatar mood
  d3.selectAll("[data-mood]").on('click.command', function(ev) {
    const e = d3.select(this);
    const mood = e.attr('data-mood');
    head.setMood( mood );
    d3.selectAll("[data-mood]").classed('selected', false);
    e.classed('selected', true);
    // Настроение тоже часть настроек: без сохранения оно сбрасывалось
    // после перезагрузки и не доезжало до окна-оверлея.
    if ( mood && !loadingConfig ) {
      cfg('avatar-mood', mood);
      saveConfig();
    }
  });

  // Avatar pose
  d3.selectAll("[data-pose]").on('click.command', async function(ev) {
    const e = d3.select(this);
    const sel = e.classed("selected");
    const repeat = d3.select("#pose-repeat").classed("selected");
    d3.selectAll("[data-pose]").classed("selected",false);
    if ( sel ) {
      head.stopPose();
    } else {
      let pose = e.attr('data-pose');
      if ( pose !== 'FILE' ) {
        d3.selectAll("[data-pose='" + pose + "']").classed("selected",repeat);
        head.playPose( pose, progressUpdate, (repeat ? 600 : 5) );
      }
    }
  });

  // Avatar gesture
  d3.selectAll("[data-gesture]").on('click.command', async function(ev) {
    const e = d3.select(this);
    const sel = e.classed("selected");
    const repeat = d3.select("#gesture-repeat").classed("selected");
    const mirror = d3.select("#gesture-mirror").classed("selected");
    d3.selectAll("[data-gesture]").classed("selected",false);
    if ( sel ) {
      head.stopGesture();
    } else {
      let g = e.attr('data-gesture');
      d3.selectAll("[data-gesture='" + g + "']").classed("selected",repeat);
      head.playGesture( g, (repeat ? 600 : 5), mirror );
    }
  });

  // Open pose file
  d3.select("#pose-file").on('change', function(ev) {
    let file = ev.target.files[0];
    var reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload =  (readerEvent) => {
      let content = readerEvent.target.result;
      const repeat = d3.select("#pose-repeat").classed("selected");
      d3.selectAll("[data-pose='FILE']").classed("selected",repeat);
      head.playPose( content, progressUpdate, (repeat ? 600 : 5) );
    }
    ev.target.value = '';
  });

  d3.selectAll("[data-animation]").on('click.command', async function(ev) {
    const e = d3.select(this);
    const sel = e.classed("selected");
    const repeat = d3.select("#animation-repeat").classed("selected");
    d3.selectAll("[data-animation]").classed("selected",false);
    if ( sel ) {
      head.stopAnimation();
    } else {
      let animation = e.attr('data-animation');
      if ( animation !== 'FILE' ) {
        d3.selectAll("[data-animation='" + animation + "']").classed("selected",repeat);
        head.playAnimation( animation, progressUpdate, (repeat ? 300 : 7) );
      }
    }
  });

  // Open animation file
  d3.select("#animation-file").on('change', function(ev) {
    let file = ev.target.files[0];
    var reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = (ev2) => {
      let content = ev2.target.result;
      const repeat = d3.select("#animation-repeat").classed("selected");
      d3.selectAll("[data-animation='FILE']").classed("selected",repeat);
      head.playAnimation( content, progressUpdate, (repeat ? 300 : 7) );
    }
    ev.target.value = '';
  });

  // Save configuration changes
  d3.selectAll("[data-item]").on('click.config input.config change.config keyup.config', function(ev) {
    const e = d3.select(this);
    const item = e.attr("data-item");
    const type = e.attr("data-type");
    const range = e.attr("data-range");
    let value;
    if ( type === 'boolean' ) {
      value = !e.classed( "selected" );
      e.classed( "selected", value );
    } else if ( type === "option" ) {
      value = e.attr("data-"+item);
    } else if ( type === "value" ) {
      if ( range !== null ) {
        value = parseFloat( e.property("value") );
      } else {
        value = e.property("value");
      }
    }
    if ( !loadingConfig && value !== undefined && cfg( item ) !== value ) {
      cfg( item, value );
      saveConfig();
    }
  });

  // Change session
  d3.selectAll("[data-session]").on('click.command', entrySelect );
  d3.selectAll("[data-entry-move]").on('click.command', entryMove );

  d3.select("#selectall").on("click", function(ev) {
    const e = d3.select(this);
    const mode = !e.classed("selected");
    e.classed("selected", mode);

    d3.select(".session.selected").selectAll(".select").nodes().forEach( (n) => {
      const x = d3.select(n);
      if ( x.classed("selected") !== mode ) {
        x.dispatch("click");
      }
    });
  });

  // Reset range
  d3.selectAll("[data-range-reset]").on('click.command', function(ev) {
    const e = d3.select(this);
    const target = e.attr("data-target");
    const value = parseFloat(e.attr("data-range-reset"));
    d3.select("[data-"+target+"]").property("value",value).dispatch("change");
  });

  // Show value labels
  d3.selectAll("[data-range]").on('input.label change.label keyup.label', function(ev) {
    const e = d3.select(this);
    const n = e.node();
    const type = e.attr("data-range");
    let v = '';
    if ( type === 'float' ) {
      v = Math.round(100 * n.value) / 100;
    } else if ( type === 'second' ) {
      v = Math.round(100 * n.value) / 100 + ' s';
    } else if ( type === 'percentage' ) {
      v = Math.round(100 * n.value) + '%';
    } else if ( type === 'px' ) {
      v = n.value + ' px';
    } else if ( type === 'meter' ) {
      v = n.value + ' m';
    } else if ( type === 'radian' ) {
      v = n.value + ' rad';
    } else if ( type === 'word' ) {
      v = n.value + ' ' + i18nWord('words');
    } else if ( type === 'dialog' ) {
      v = n.value + ' ' + i18nWord('dialogs');
    }
    if ( n.nextElementSibling ) n.nextElementSibling.textContent = v;
  });

  d3.selectAll("[data-item='name']").on('input.command change.command keyup.command', function(ev) {
    const e = d3.select(this);
    const name = e.property("value");
    d3.select("#entry-"+CFG.session).select("div").text( name );
    d3.select("#name").text( name );
  });

  // Show/hide pages
  d3.selectAll("[data-show]").on('click.command', function(ev) {
    const e = d3.select(this);
    const sel = e.classed("selected");
    const show = e.attr("data-show");
    const parts = show.split("-");

    d3.selectAll("[data-show*='" + parts[0] + "-']").classed("selected", false);
    d3.selectAll("[id*='" + parts[0] + "-']").classed("hidden", true);
    if ( !sel ) {
      e.classed("selected",true);
      d3.select('#'+show)
        .classed("hidden", false)
        .selectAll("textarea").dispatch("keyup");
      if ( parts[0] === 'right' ) {
        d3.select("#right-sessions").classed("hidden", true);
      }
    } else {
      if ( parts[0] === 'right' ) {
        d3.select("#right-sessions").classed("hidden", false);
      }
    }
  });

  d3.selectAll("[data-theme-ratio]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-theme-ratio]").classed('selected',false);
    e.classed('selected',true);
    const main = d3.select("#main");
    d3.selectAll("[data-theme-ratio]").nodes().forEach( x => {
      main.classed("ratio-" + x.dataset.themeRatio, false);
    });
    let ratio = 'ratio-' + d3.select("[data-theme-ratio].selected").attr('data-theme-ratio');
    main.classed(ratio, true);
  });

  d3.selectAll("[data-theme-layout]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-theme-layout]").classed('selected',false);
    e.classed('selected',true);
    const main = d3.select("#main");
    d3.selectAll("[data-theme-layout]").nodes().forEach( x => {
      main.classed("layout-" + x.dataset.themeLayout, false);
    });
    let layout = 'layout-' + d3.select("[data-theme-layout].selected").attr('data-theme-layout');
    main.classed(layout, true);
  });

  d3.selectAll("[data-theme-brightness]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-theme-brightness]").classed('selected',false);
    e.classed('selected',true);
    const body = d3.select("body");
    d3.selectAll("[data-theme-brightness]").nodes().forEach( x => {
      body.classed("theme-" + x.dataset.themeBrightness, false);
    });
    let theme = 'theme-' + d3.select("[data-theme-brightness].selected").attr('data-theme-brightness');
    body.classed(theme, true);
  });

  d3.selectAll("[data-avatar-name]").on('click.command', async function(ev) {
    if (!head) return;
    const e = d3.select(this);
    d3.selectAll("[data-avatar-name]").classed('selected',false);
    let avatar = e.attr('data-avatar-name');
    d3.selectAll("[data-avatar-name='" + avatar + "']").classed("selected",true);
    // Выбор модели тоже сохраняем: раньше он жил только до перезагрузки.
    if (avatar && avatar !== 'FILE') {
      cfg('avatar-name', avatar);
      saveConfig();
    }
    if ( avatar === 'FILE' ) {
      // Do nothing
    } else if ( avatar === 'URL' ) {
      let url = cfg('avatar-url');
      d3.selectAll("[data-pose],[data-animation],#pause").classed("selected",false);
      const o = {
        url: url,
        body: cfg('avatar-body')
      };
      if ( url !== head.avatar?.url ) {
        try {
          await head.showAvatar( o, progressUpdate );
          headLoaded();
          d3.select("[data-camera-frame].selected").dispatch('click');
        } catch(error) {
          errorShow(error);
        }
      }
    } else {
      const name = e.attr('data-avatar-name');
      d3.selectAll("[data-pose],[data-animation],#pause").classed("selected",false);
      if ( site.avatars[name].url !== head.avatar?.url ) {
        try {
          await head.showAvatar( site.avatars[name], progressUpdate );
          headLoaded();
          d3.select("[data-camera-frame].selected").dispatch('click');
        } catch(error) {
          errorShow(error);
        }
      }
    }
  });

  // === 3D-аватар: по умолчанию выключен ===
  // Переключатель в панели «Avatar». Движок (three.js + VRM) тяжёлый, поэтому
  // при старте страницы модель не грузим, пока пользователь не включит.
  d3.select('#avatar3dSwitch').on('click', () => setAvatar3D(!avatar3dEnabled()));
  syncAvatar3dUi();

  // Open avatar file
  d3.select("#avatar-name-file").on('change', async function(ev) {
    let file = ev.target.files[0];
    var reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = async (ev2) => {
      d3.selectAll("[data-pose],[data-animation],#pause").classed("selected",false);
      const o = {
        url: ev2.target.result,
        body: cfg('avatar-body')
      };
      try {
        await head.showAvatar( o, progressUpdate );
        headLoaded();
        d3.select("[data-camera-frame].selected").dispatch('click');
      } catch(error) {
        errorShow(error);
      }
    }
    ev.target.value = '';
  });

  d3.selectAll("[data-avatar-brightness],[data-avatar-contrast],[data-avatar-saturate]").on('input.command change.command keyup.command', function(ev) {
    const e = d3.select(this);
    let filters = [];
    d3.selectAll("[data-avatar-brightness],[data-avatar-contrast],[data-avatar-saturate]").nodes().forEach( node => {
      filters.push( (node.dataset.avatarBrightness || node.dataset.avatarContrast || node.dataset.avatarSaturate) + '(' + node.value + ')' );
    });
    d3.select("#avatar").style("filter", filters.join(' ') );
    // Значение сохраняем, чтобы фильтр не сбрасывался после перезагрузки.
    const key = e.attr('data-item');
    if (key && !loadingConfig) {
      cfg(key, parseFloat(e.property('value')));
      saveConfig();
    }
  });

  d3.selectAll("[data-avatar-body]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-avatar-body]").classed('selected',false);
    e.classed('selected',true);
    const body = e.attr('data-avatar-body');
    if (body && !loadingConfig) {
      cfg('avatar-body', body);
      saveConfig();
      applyAvatarSettings();
    }
  });

  // Ручной ввод URL модели и смена URL должны применяться на лету, а не
  // только после перезагрузки страницы.
  d3.selectAll("[data-item='avatar-url']").on('change.config', function() {
    if (!loadingConfig) applyAvatarSettings();
  });

  d3.selectAll("[data-view-image]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-view-image]").classed('selected',false);
    let view = e.attr('data-view-image');
    d3.selectAll("[data-view-image='" + view + "']").classed("selected",true);
    if ( view === 'NONE' ) {
      CFG.sessions[CFG.session].view.image = "NONE";
      d3.select("#main")
        .classed("presence-video",false)
        .classed("presence-vr", true);
      d3.select("#view-video").attr("src","");
      d3.select("#video").node().load();
      d3.select("#view").style("background-image","none" );
    } else if ( view === 'FILE' ) {
      // Do nothing
    } else if ( view === 'URL' ) {
      CFG.sessions[CFG.session].view.image = "URL";
      d3.select("#main")
        .classed("presence-video", true)
        .classed("presence-vr", false);
      d3.select("#view-video").attr("src","");
      d3.select("#video").node().load();
      const image = cfg('view-url');
      d3.select("#view").style("background-image","url(" + image + ")" );
    } else {
      CFG.sessions[CFG.session].view.image = view;
      d3.select("#main")
        .classed("presence-video", true)
        .classed("presence-vr", false);
      const image = e.attr('data-view-image');
      const type = e.attr('data-image-type');
      if ( type.startsWith('video/') ) {
        d3.select("#view-video")
          .attr("src", image )
          .attr("type", type );
        d3.select("#video").node().load();
        d3.select("#view").style("background-image","none" );
      } else if ( type.startsWith('image/') ) {
        d3.select("#view-video").attr("src","");
        d3.select("#video").node().load();
        d3.select("#view").style("background-image","url(" + image + ")" );
      }
    }
  });

  // Open view image file
  d3.select("#view-image-file").on('change', function(ev) {
    let file = ev.target.files[0];
    var reader = new FileReader();
    if ( file.type.startsWith('image/') ) {
      d3.select("#main")
        .classed("presence-video", true)
        .classed("presence-vr", false);
      reader.readAsDataURL(file);
      reader.onload = (ev2) => {
        d3.select("#view-video").attr("src","");
        d3.select("#video").node().load();
        d3.select("#view").style("background-image","url(" + ev2.target.result + ")" );
      }
    } else if ( file.type.startsWith('video/') ) {
      d3.select("#main")
        .classed("presence-video", true)
        .classed("presence-vr", false);
      reader.readAsDataURL(file);
      reader.onload = (ev2) => {
        let content = ev2.target.result;
        d3.select("#view-video").attr("src",content);
        d3.select("#video").node().load();
      }
    }
    ev.target.value = '';
  });

  d3.select("[data-pose-movement]").on('input.command change.command keyup.command', function(ev) {
    let factor = parseFloat( d3.select("[data-pose-movement]").property("value") );
    head.opt.modelMovementFactor = factor;
    head.setPoseFromTemplate( head.poseCurrentTemplate );
  });

  d3.selectAll("[data-camera-frame]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-camera-frame]").classed('selected',false);
    const camera = e.attr('data-camera-frame');
    d3.selectAll("[data-camera-frame='" + camera + "']").classed("selected",true);
    const opt = {
      cameraX: parseFloat( cfg("camera-x") || 0 ),
      cameraY: parseFloat( cfg("camera-y") || 0 ),
      cameraDistance: parseFloat( cfg("camera-d") || 0 ),
      cameraRotateX: parseFloat( cfg("camera-rotx") || 0 ),
      cameraRotateY: parseFloat( cfg("camera-roty") || 0 )
    };
    head.setView( camera, opt );
    // Ракурс сохраняем: окно-оверлей при следующем применении настроек
    // должно встать в тот же вид, а не в дефолтный.
    if ( camera && !loadingConfig ) {
      cfg('camera-frame', camera);
      saveConfig();
    }
  });

  d3.selectAll("[data-camera-x],[data-camera-y],[data-camera-d],[data-camera-rotx],[data-camera-roty]").on('input.command change.command keyup.command', function(ev) {
    d3.select("[data-camera-frame].selected").dispatch('click');
  });

  d3.select("[data-camera-fps]").on('input.command change.command keyup.command', function(ev) {
    let fps = parseFloat( d3.select("[data-camera-fps]").property("value") );
    head.opt.modelFPS = fps;
    head.animFrameDur = 1000 / fps;
  });

  function setLightingData() {
    let o = {
      lightAmbientColor: d3.select("[data-light-ambient-color]").property("value"),
      lightAmbientIntensity: parseFloat( d3.select("[data-light-ambient-intensity]").property("value") ),
      lightDirectColor: d3.select("[data-light-direct-color]").property("value"),
      lightDirectIntensity: parseFloat( d3.select("[data-light-direct-intensity]").property("value") ),
      lightDirectPhi: parseFloat( d3.select("[data-light-direct-phi]").property("value") ),
      lightDirectTheta: parseFloat( d3.select("[data-light-direct-theta]").property("value") ),
      lightSpotColor: d3.select("[data-light-spot-color]").property("value"),
      lightSpotIntensity: parseFloat( d3.select("[data-light-spot-intensity]").property("value") ),
      lightSpotPhi: parseFloat( d3.select("[data-light-spot-phi]").property("value") ),
      lightSpotTheta: parseFloat( d3.select("[data-light-spot-theta]").property("value") ),
      lightSpotDispersion: parseFloat( d3.select("[data-light-spot-dispersion]").property("value") )
    };
    d3.select('#lightingdata').property("value", JSON.stringify( o ) );
  }

  d3.selectAll("[data-light-ambient-color],[data-light-ambient-intensity]").on('input.command change.command keyup.command', function(ev) {
    const e = d3.select(this);
    let o = {
      lightAmbientColor: d3.select("[data-light-ambient-color]").property("value"),
      lightAmbientIntensity: parseFloat( d3.select("[data-light-ambient-intensity]").property("value") )
    };
    head.setLighting(o);
    setLightingData();
  });

  d3.selectAll("[data-light-direct-color],[data-light-direct-intensity],[data-light-direct-phi],[data-light-direct-theta]").on('input.command change.command keyup.command', function(ev) {
    const e = d3.select(this);
    let o = {
      lightDirectColor: d3.select("[data-light-direct-color]").property("value"),
      lightDirectIntensity: parseFloat( d3.select("[data-light-direct-intensity]").property("value") ),
      lightDirectPhi: parseFloat( d3.select("[data-light-direct-phi]").property("value") ),
      lightDirectTheta: parseFloat( d3.select("[data-light-direct-theta]").property("value") )
    };
    head.setLighting(o);
    setLightingData();
  });

  d3.selectAll("[data-light-spot-color],[data-light-spot-intensity],[data-light-spot-phi],[data-light-spot-theta],[data-light-spot-dispersion]").on('input.command change.command keyup.command', function(ev) {
    const e = d3.select(this);
    let o = {
      lightSpotColor: d3.select("[data-light-spot-color]").property("value"),
      lightSpotIntensity: parseFloat( d3.select("[data-light-spot-intensity]").property("value") ),
      lightSpotPhi: parseFloat( d3.select("[data-light-spot-phi]").property("value") ),
      lightSpotTheta: parseFloat( d3.select("[data-light-spot-theta]").property("value") ),
      lightSpotDispersion: parseFloat( d3.select("[data-light-spot-dispersion]").property("value") ),
    };
    head.setLighting(o);
    setLightingData();
  });

  d3.selectAll("[data-view-brightness],[data-view-contrast],[data-view-saturate],[data-view-blur]").on('input.command change.command keyup.command', function(ev) {
    const e = d3.select(this);
    let filters = [];
    d3.selectAll("[data-view-brightness],[data-view-contrast],[data-view-saturate],[data-view-blur]").nodes().forEach( node => {
      filters.push( (node.dataset.viewBrightness || node.dataset.viewContrast || node.dataset.viewSaturate || node.dataset.viewBlur) + '(' + node.value + (node.dataset.viewBlur ? 'px' :'') + ')' );
    });
    d3.select("#view").style("filter", filters.join(' ') );
  });

  d3.selectAll("[data-voice-type]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-voice-type]").classed('selected',false);
    e.classed('selected',true);
    d3.select("#voice-google").style("display", (e.attr("data-voice-type") === 'google') ? "flex" : "none" );
    d3.select("#voice-eleven").style("display", (e.attr("data-voice-type") === 'eleven') ? "flex" : "none" );
    d3.select("#voice-microsoft").style("display", (e.attr("data-voice-type") === 'microsoft') ? "flex" : "none" );
    d3.select("#voice-silero").style("display", (e.attr("data-voice-type") === 'silero') ? "flex" : "none" );
    d3.select("#lipsync").style("display", (e.attr("data-voice-type") !== 'microsoft') ? "flex" : "none" );
    d3.select("#excludes").style("display", (e.attr("data-voice-type") === 'google') ? "flex" : "none" );
  });

  d3.selectAll("[data-voice-google-id]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-voice-google-id]").classed('selected',false);
    e.classed('selected',true);
  });

  d3.selectAll("[data-voice-eleven-id]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-voice-eleven-id]").classed('selected',false);
    e.classed('selected',true);
  });

  d3.selectAll("[data-voice-microsoft-id]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-voice-microsoft-id]").classed('selected',false);
    e.classed('selected',true);
  });

  d3.selectAll("[data-voice-lipsync-lang]").on('click.command click.config', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-voice-lipsync-lang]").classed('selected',false);
    e.classed('selected',true);
    d3.select('#visemeword').dispatch('change');
    // Сохраняем в cfg вручную (специфичный command-хендлер перебивает общий config).
    const item = e.attr('data-item');
    if (item) {
      const value = e.attr('data-' + item);
      if (value && !loadingConfig && cfg(item) !== value) {
        cfg(item, value);
        saveConfig();
      }
    }
  });

  d3.selectAll("[data-voice-silero-lang]").on('click.command click.config', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-voice-silero-lang]").classed('selected',false);
    e.classed('selected',true);
    const item = e.attr('data-item');
    if (item) {
      const value = e.attr('data-' + item);
      if (value && !loadingConfig && cfg(item) !== value) {
        cfg(item, value);
        saveConfig();
      }
    }
  });

  d3.selectAll("[data-voice-silero-speaker]").on('click.command click.config', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-voice-silero-speaker]").classed('selected',false);
    e.classed('selected',true);
    // Сохраняем в cfg вручную (специфичный command-хендлер перебивает общий config).
    const item = e.attr('data-item');
    if (item) {
      const value = e.attr('data-' + item);
      if (value && !loadingConfig && cfg(item) !== value) {
        cfg(item, value);
        saveConfig();
      }
    }
  });

  d3.select("[data-voice-mixerbg]").on('input.command change.command keyup.command', function(ev) {
    if (!head) return;
    let gain = parseFloat( d3.select("[data-voice-mixerbg]").property("value") );
    head.setMixerGain( null, gain );
  });

  d3.select("[data-voice-mixerspeech]").on('input.command change.command keyup.command', function(ev) {
    if (!head) return;
    let gain = parseFloat( d3.select("[data-voice-mixerspeech]").property("value") );
    head.setMixerGain( gain, null );
  });

  d3.selectAll("[data-voice-reverb]").on('click.command', function(ev) {
    if (!head) return;
    const e = d3.select(this);
    d3.selectAll("[data-voice-reverb]").classed('selected',false);
    e.classed('selected',true);
    let reverb = e.attr('data-voice-reverb');
    if ( reverb === 'NONE' ) {
      head.setReverb(null);
    } else {
      head.setReverb(reverb);
    }
  });

  d3.selectAll("[data-voice-background]").on('click.command', async function(ev) {
    if (!head) return;
    const e = d3.select(this);
    d3.selectAll("[data-voice-background]").classed("selected",false);
    let background = e.attr('data-voice-background');
    d3.selectAll("[data-voice-background='" + background + "']").classed("selected",true);
    if ( background === 'NONE' ) {
      head.stopBackgroundAudio();
    } else if ( background === 'FILE' ) {
      // Do nothing
    } else {
      head.playBackgroundAudio(background);
    }
  });

  // Whisper
  d3.selectAll("[data-whisper-type]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-whisper-type]").classed('selected',false);
    e.classed('selected',true);
  });

  // Локальная Whisper-модель
  d3.selectAll("[data-whisper-local-model]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-whisper-local-model]").classed('selected',false);
    e.classed('selected',true);
  });

  // Язык Whisper
  d3.selectAll("[data-whisper-language]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-whisper-language]").classed('selected',false);
    e.classed('selected',true);
  });

  // Кнопка «Загрузить модель» (lazy load в память)
  d3.select("#stt-load").on('click', async function(ev) {
    const model = cfg('whisper-local-model') || 'turbo';
    const lang = cfg('whisper-language') || 'ru';
    const st = d3.select('#stt-status');
    st.text('STT: загружаю ' + model + '...');
    try {
      const r = await fetch('/api/stt/load', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + await jwtGet() },
        body: JSON.stringify({ model, language: lang })
      });
      const j = await r.json();
      if (j.ok) {
        st.text('STT: ' + model + ' готов (' + lang + ')');
      } else {
        st.text('STT: ошибка — ' + (j.error || 'unknown'));
      }
    } catch (e) {
      st.text('STT: ' + e.message);
    }
  });

  // Кнопка «Скачать модель» (показывает инструкцию, реальная загрузка — через download_whisper.py)
  d3.select("#stt-download").on('click', async function(ev) {
    const model = cfg('whisper-local-model') || 'turbo';
    const st = d3.select('#stt-status');
    st.text('STT: запуск download_whisper.py ' + model + '...');
    try {
      // Запускаем фоновую загрузку. Серверный эндпоинт /api/stt/download
      const r = await fetch('/api/stt/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + await jwtGet() },
        body: JSON.stringify({ model })
      });
      const j = await r.json();
      st.text('STT: ' + (j.message || j.error || 'ok'));
    } catch (e) {
      st.text('STT: запустите ./venv/bin/python3 download_whisper.py --model ' + model);
    }
  });

  // === Wake Word ===
  // Состояние
  window._wakeword = {
    enabled: false,
    word: 'финик',
    threshold: 0.5,
    pausedUntil: 0,         // не слушаем, пока аватар говорит / идёт запись
    audioCtx: null,
    stream: null,
    source: null,
    processor: null,
    detector: null,         // openWakeWord
    detecting: false,
  };

  // Загрузить openWakeWord модель с сервера (wasm+onnx)
  async function wakewordLoadModel() {
    const status = d3.select('#wakeword-status');
    status.text('WakeWord: загружаю модели openWakeWord...');
    try {
      // Подгружаем openwakeword JS-биндинг с CDN или локально
      if (!window._oww) {
        await import('https://cdn.jsdelivr.net/npm/@picovoice/picovoice-web-+/dist/index.js').catch(()=>null);
      }
      // Используем серверный прокси для детекции
      const w = (cfg('wakeword-word') || 'финик').toLowerCase();
      window._wakeword.word = w;
      status.text('WakeWord: готов (серверный режим), слово="' + w + '"');
      return true;
    } catch (e) {
      status.text('WakeWord: ошибка ' + e.message);
      return false;
    }
  }

  // Серверный wake word: отправляем чанки PCM на /api/wakeword/listen
  async function wakewordStart() {
    if (window._wakeword.detecting) return;
    const status = d3.select('#wakeword-status');
    const word = (cfg('wakeword-word') || 'финик').toLowerCase();
    window._wakeword.word = word;
    window._wakeword.threshold = parseFloat(cfg('wakeword-threshold')) || 0.5;
    window._wakeword.pausedUntil = 0;

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
      window._wakeword.stream = stream;
      const audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      window._wakeword.audioCtx = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      window._wakeword.source = source;

      // Processor для сэмплов 16 kHz int16
      const bufSize = 4096;
      const processor = audioCtx.createScriptProcessor(bufSize, 1, 1);
      window._wakeword.processor = processor;
      window._wakeword.detecting = true;

      // Чанк 0.5 с: сервер получает аудио вдвое быстрее, слово срабатывает
      // раньше (раньше отправлялась секунда, и только она добавляла до 1 с).
      const chunkLen = 8000;
      let samples = [];

      processor.onaudioprocess = async (e) => {
        if (!window._wakeword.detecting) return;
        // Пока аватар говорит (свой TTS) или сразу после срабатывания —
        // не слушаем: иначе эхо снова запускает запись.
        const speaking = !!(window.head && window.head.isSpeaking);
        if (speaking || Date.now() < (window._wakeword.pausedUntil || 0)) {
          samples.length = 0;
          return;
        }
        const ch = e.inputBuffer.getChannelData(0);
        // Конвертим float32 → int16
        for (let i = 0; i < ch.length; i++) {
          let s = Math.max(-1, Math.min(1, ch[i]));
          samples.push(s < 0 ? s * 0x8000 : s * 0x7fff);
        }
        if (samples.length >= chunkLen) {
          const chunk = new Int16Array(samples.splice(0, chunkLen));
          try {
            const r = await fetch('/api/wakeword/listen', {
              method: 'POST',
              headers: { 'Content-Type': 'audio/pcm', 'Authorization': 'Bearer ' + await jwtGet() },
              body: chunk.buffer
            });
            const j = await r.json().catch(() => ({}));
            if (j.detected) {
              console.log('[WakeWord] detected:', j.word);
              // Пауза на время озвучки и записи команды: иначе ответ
              // аватара и сама команда снова попадают в детектор.
              window._wakeword.pausedUntil = Date.now() + 6000;
              samples.length = 0;
              window.dispatchEvent(new CustomEvent('wakeword', { detail: j }));
              // Прерывание генерации при необходимости
              if ((cfg('wakeword-interrupt') || 'on') === 'on' && typeof aiController !== 'undefined' && aiController) {
                try { aiController.abort(); } catch (e) {}
              }
            }
          } catch (e) {
            // ignore
          }
        }
      };

      source.connect(processor);
      processor.connect(audioCtx.destination);
      status.text('WakeWord: слушаю... слово="' + word + '"');
    } catch (e) {
      d3.select('#wakeword-status').text('WakeWord: ошибка микрофона — ' + e.message);
      window._wakeword.detecting = false;
    }
  }

  function wakewordStop() {
    window._wakeword.detecting = false;
    try { window._wakeword.processor && window._wakeword.processor.disconnect(); } catch(e) {}
    try { window._wakeword.source && window._wakeword.source.disconnect(); } catch(e) {}
    try { window._wakeword.audioCtx && window._wakeword.audioCtx.close(); } catch(e) {}
    try { window._wakeword.stream && window._wakeword.stream.getTracks().forEach(t => t.stop()); } catch(e) {}
    window._wakeword.processor = null;
    window._wakeword.source = null;
    window._wakeword.audioCtx = null;
    window._wakeword.stream = null;
    d3.select('#wakeword-status').text('WakeWord: выключен');
  }

  // Обработчик обнаружения wake word: запускаем запись голоса → STT → чат
  window.addEventListener('wakeword', async (ev) => {
    console.log('[WakeWord] event:', ev.detail);
    // 1) Прерываем текущую генерацию
    if ((cfg('wakeword-interrupt') || 'on') === 'on' && aiController) {
      try { aiController.abort(); } catch (e) {}
    }
    // 2) Озвучиваем короткий ответ и сразу начинаем слушать
    try { head.speakText('Слушаю.', { lipsyncLang: 'ru', ttsLang: 'ru' }); } catch(e) {}
    // 3) Запускаем запись с авто-стопом по тишине
    setTimeout(() => { if (typeof recordingRecord === 'function') recordingRecord(); }, 300);
  });

  d3.select("#wakeword-toggle").on('click', async function(ev) {
    if (!window._wakeword.detecting) {
      await wakewordStart();
      d3.select(this).classed('selected', true).text('Включен');
    } else {
      wakewordStop();
      d3.select(this).classed('selected', false).text('Выключен');
    }
  });

  d3.select("#wakeword-train").on('click', async function(ev) {
    const word = (cfg('wakeword-word') || 'финик').toLowerCase();
    d3.select('#wakeword-status').text('WakeWord: обучение на "' + word + '"...');
    try {
      const r = await fetch('/api/wakeword/train', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + await jwtGet() },
        body: JSON.stringify({ word, threshold: parseFloat(cfg('wakeword-threshold')) || 0.5 })
      });
      const j = await r.json();
      d3.select('#wakeword-status').text('WakeWord: ' + (j.message || j.error || 'ok'));
    } catch (e) {
      d3.select('#wakeword-status').text('WakeWord: обучение — ' + e.message);
    }
  });

  // Периодический опрос gateway для серверных анимаций/команд
  if (!window._gatewayPollStarted) {
    window._gatewayPollStarted = true;
    setInterval(async () => {
      try {
        const r = await fetch('/api/avatar/gateway/poll?max=8', {
          headers: { 'Authorization': 'Bearer ' + await jwtGet() }
        });
        if (!r.ok) return;
        const j = await r.json();
        const events = j.events || [];
        for (const ev of events) {
          if (ev.type === 'motion' && ev.name && typeof window.playMotion === 'function') {
            console.log('[Gateway] motion:', ev.name);
            try { await window.playMotion(ev.name); } catch (e) {}
          } else if (ev.type === 'emotion' && ev.name && head.setMood) {
            try { head.setMood(ev.name); } catch (e) {}
          } else if (ev.type === 'tts' && ev.text && head.speakText) {
            try { await head.speakText(ev.text, { lipsyncLang: ev.lang || 'ru', ttsLang: ev.lang || 'ru' }); } catch (e) {}
          }
        }
      } catch (e) { /* ignore */ }
    }, 1500);
  }

  // Регистрация пользовательских анимаций
  async function refreshUserAnimations() {
    try {
      const r = await fetch('/api/avatar/motions', {
        headers: { 'Authorization': 'Bearer ' + await jwtGet() }
      });
      if (!r.ok) return;
      const j = await r.json();
      const motions = j.motions || {};
      const names = Object.keys(motions);
      if (names.length && typeof motionEngine !== 'undefined' && motionEngine) {
        motionEngine.registerMotions(motions);
        console.log('[Motions] registered user motions:', names);
      }
      // Обновить UI список
      const list = d3.select('#usermotions-list');
      if (!list.empty()) {
        list.selectAll('*').remove();
        names.forEach(name => {
          const c = list.append('div')
            .classed('command', true)
            .attr('data-user-motion', name)
            .text(name)
            .attr('title', motions[name]?._description || name)
            .style('padding', '4px 8px')
            .style('font-size', '12px');
          c.on('click', async () => {
            if (window.motionEngine && typeof window.motionEngine.play === 'function') {
              try { await window.motionEngine.play(name); }
              catch(e) { console.error('play err', e); alert('Ошибка: ' + e.message); }
            } else if (window.playMotion) {
              try { await window.playMotion(name); } catch(e) {}
            }
          });
          // Контекстное меню для удаления (правый клик)
          c.on('contextmenu', async (ev) => {
            ev.preventDefault();
            if (!confirm(`Удалить анимацию "${name}"?`)) return;
            try {
              await fetch('/api/avatar/motion/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + await jwtGet() },
                body: JSON.stringify({ name })
              });
              await refreshUserAnimations();
            } catch (e) { console.error('delete err', e); }
          });
        });
      }
    } catch (e) {}
  }
  refreshUserAnimations();

  // Кнопка сохранения пользовательской анимации (расширение существующего saveBoneAnim)
  // уже реализована в `saveBoneAnim` (portable.js:3733+), но дёргает /api/avatar/motion/create,
  // который теперь работает.

  // Кнопка загрузки .fbx/.glb/.vrma/.json в MotionEngine
  if (!window._uploadAnimHooked) {
    window._uploadAnimHooked = true;
    document.addEventListener('change', async (ev) => {
      const t = ev.target;
      if (!t || t.id !== 'upload-motion-file') return;
      const file = t.files && t.files[0];
      if (!file) return;
      const ext = (file.name.split('.').pop() || '').toLowerCase();
      if (!['fbx','glb','gltf','vrma','json'].includes(ext)) {
        alert('Поддерживаются: .fbx, .glb, .gltf, .vrma, .json');
        return;
      }
      const form = new FormData();
      form.append('file', file);
      form.append('name', file.name.replace(/\.[^.]+$/, '').toLowerCase().replace(/[^a-z0-9_-]+/g, '_'));
      try {
        const r = await fetch('/api/avatar/motion/upload', {
          method: 'POST', body: form,
          headers: { 'Authorization': 'Bearer ' + await jwtGet() }
        });
        const j = await r.json();
        if (j.ok) {
          if (ext === 'json' && typeof motionEngine !== 'undefined') {
            try {
              const resp2 = await fetch(j.motion.url || ('/api/avatar/motion/file/' + j.motion.name + '.json'));
              const data = await resp2.json();
              motionEngine.registerMotions({ [j.motion.name]: data });
            } catch (e) {}
          } else if (typeof head !== 'undefined' && head.playAnimation) {
            try { await head.playAnimation(j.motion.url, head.armature, 1.0); } catch (e) {}
          }
          await refreshUserAnimations();
        } else {
          alert('Upload error: ' + (j.error || 'unknown'));
        }
      } catch (e) {
        alert('Upload error: ' + e.message);
      } finally {
        t.value = '';
      }
    });
  }

  // Dynamic bones
  d3.select("[data-dynamicbones-sensitivity]").on('input.command change.command keyup.command', function(ev) {
    let factor = parseFloat( d3.select("[data-dynamicbones-sensitivity]").property("value") );
    head.dynamicbones.setOptionValue("sensitivityFactor",factor);
  });

  d3.select("[data-dynamicbones-movement]").on('input.command change.command keyup.command', function(ev) {
    let factor = parseFloat( d3.select("[data-dynamicbones-movement]").property("value") );
    head.dynamicbones.setOptionValue("movementFactor",factor);
  });

  // Open voice background file
  d3.select("#voice-background-file").on('change', function(ev) {
    let file = ev.target.files[0];
    var reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = (ev2) => {
      if (!head) return;
      let content = ev2.target.result;
      head.playBackgroundAudio(content);
    }
    ev.target.value = '';
  });

  d3.selectAll("[data-ai-model]").on('click.command', function(ev) {
    const e = d3.select(this);
    d3.selectAll("[data-ai-model]").classed('selected',false);
    e.classed('selected',true);
    d3.select("#ai-openai").style("display", (e.attr("data-ai-model").startsWith('gpt') ) ? "flex" : "none" );
    d3.select("#ai-gemini").style("display", (e.attr("data-ai-model").startsWith('gemini') ) ? "flex" : "none" );
    d3.select("#ai-grok").style("display", (e.attr("data-ai-model").startsWith('grok') ) ? "flex" : "none" );
    d3.select("#ai-llama").style("display", (e.attr("data-ai-model").startsWith('llama') ) ? "flex" : "none" );
    d3.select("#ai-lmstudio").style("display", (e.attr("data-ai-model") === 'lmstudio' ) ? "flex" : "none" );
  });

  d3.select("#session-duplicate").on('click', function(ev) {
    let id = CFG.sessions.length;
    let clone = JSON.parse( JSON.stringify(cfg()) );
    clone.name = 'Nimetön';
    CFG.sessions.push(clone);
    saveConfig();
    loadConfig(id);
  });

  d3.select("#session-delete").on('click', function(ev) {
    if ( CFG.sessions.length > 1 ) {
      let id = CFG.session;
      CFG.sessions.splice(id,1);
      if ( id > 0 ) id--;
      saveConfig();
      loadConfig(id);
    }
  });

  d3.select("#delete").on("click", function(ev) {
    d3.select("#selectall").classed("selected",false);
    d3.select(".session.selected").selectAll(".select.selected").nodes().forEach( (n) => {
      d3.select(n.parentElement.parentElement).remove();
    });
  });

  d3.select("#flag").on("click", async function(ev) {
    let msgs = openaiBuildMessage();
    let flag = await openaiModerateMessage(msgs);
  });

  d3.select('#copy').on('click', function(ev) {
  const session = d3.select("#right-sessions .session.selected");
    const ms = [];

    [ 'ai-openai-system', 'ai-openai-user1', 'ai-openai-ai1',
      'ai-openai-user2', 'ai-openai-ai2' ].forEach( x => {
      let p = cfg(x);
      if ( p && p.length ) ms.push(p);
    });

    session.selectAll(".message").nodes().forEach( (d) => {
      let e = d3.select(d);
      let md = e.attr("data-input") || e.attr("data-output");
      ms.push( md );
    } );

    const textarea = document.createElement('textarea');
    textarea.value = ms.join("\n\n");
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
  });

  d3.select(window).on('resize.updatesvg', function(ev) {
    d3.selectAll("textarea").dispatch("keyup");
  });

  d3.selectAll("#json").on("blur", function(ev) {
    let e = d3.select(this);
    const json = e.property("value");
    try {
      const c = JSON.parse( json );
      CFG.sessions[CFG.session] = c;
      saveConfig();
      loadConfig();
      i18nTranslate();
    } catch(error) {
      console.error(error);
    }
  });

  // Motion (experimental)
  d3.selectAll("#motion").on("click", function(ev) {
    const e = d3.select(this);
    const sel = e.classed("selected");
    e.classed("selected", !sel );
  });

  // Record
  d3.select('#record').on('click', async function(ev) {
    const x = d3.select(this);
    const mode = !x.classed("selected");
    x.classed("selected",mode);
    // recordingPlaySound();
    if ( mode ) {
      recordingRecord();
    } else {
      if ( recordingMediaRecorder ) {
        if ( recordingMediaRecorder.state === 'recording' ) {
          recordingIgnoreData = true;
          recordingMediaRecorder.stop();
        }
        recordingMediaRecorder.stream.getTracks().forEach( track => track.stop() );
        recordingMediaRecorder = null;
      }
      if (head) head.stopListening();
    }
  });

  // Viseme tester
  d3.select('#visemeword').on('input change keyup paste', function(ev) {
    const e = d3.select(this);
    const word = e.property("value");
    if ( word ) {
      try {
        const preprocessed = head.lipsyncPreProcessText( word, cfg('voice-lipsync-lang')  );
        const visemes = head.lipsyncWordsToVisemes( preprocessed, cfg('voice-lipsync-lang') );
        d3.select('#visemedata').property("value", JSON.stringify( visemes ) ).dispatch("change");
      } catch(error) {
        d3.select('#visemedata').property("value", error ).dispatch("change");
      }
    } else {
      d3.select('#visemedata').property("value", "" ).dispatch("change");
    }
  });

  // Script
  d3.select("#script").on('input change keyup paste', scriptInit);
  d3.select("body").on('keydown', scriptCommand);

  // Pause animation when not visible
  document.addEventListener("visibilitychange", async function (ev) {
    if (!head) return;
    if (document.visibilityState === "visible") {
      if ( !d3.select("#pause").classed("selected") ) {
        head.start();
      }
    } else {
      head.stop();
    }
  });

  // API keys
  d3.select("#apikey-gtts").on('input change keyup paste', function (ev) {
    if (!head) return;
    const e = d3.select(this);
    const apikey = e.property("value");

    // Change the API key
    if ( apikey ) {
      head.opt.ttsEndpoint = location.origin + "/api/tts";
      head.opt.ttsEngine = "silero";
      head.opt.ttsApikey = null;
      head.opt.jwtGet = null;
    } else {
      head.opt.ttsEndpoint = location.origin + "/api/tts";
      head.opt.ttsEngine = "silero";
      head.opt.ttsApikey = null;
      head.opt.jwtGet = null;
    }
  });

  d3.select("#apikey-microsoft").on('input change keyup paste', function (ev) {
    microsoftSpeak(null); // Closes the current connection, if any
  });

  if (!skip3D) try {

    // Show last avatar
    let o = {};
    let name = cfg('avatar-name') || Object.values(site.avatars)[0].name;
    if ( site.avatars.hasOwnProperty(name) ) {
      o = site.avatars[name];
    } else if ( name === 'URL' ) {
      o.url = cfg('avatar-url');
      o.body = cfg('avatar-body');
    } else {
      o =  Object.values(site.avatars)[0];
    }
    await head.showAvatar( o, progressUpdate);
    headLoaded(true);
  } catch (error) {
    errorShow(error);

    // Backup plan: show the first avatar in site config
    await head.showAvatar( Object.values(site.avatars)[0], progressUpdate);
    headLoaded(true);
  }

  // Fallback: always show UI after 3 seconds even if something failed
  setTimeout( () => {
    d3.selectAll('.starttransparent').style('opacity','1');
  }, 3000);

  // ========== TEMPLATE COMMANDS UI ==========

  // Show suggestions after AI sends response
  window.showTemplateSuggestions = function() {
    console.log('[TEMPLATE] showTemplateSuggestions called, pending:', window._pendingSuggestions?.length);
    if (window._pendingSuggestions && window._pendingSuggestions.length > 0) {
      showSuggestions(window._pendingSuggestions);
    }
  };

  window.refreshTemplateList = async function() {
    try {
    const resp = await fetch('/api/templates');
    if (!resp.ok) return;
    const data = await resp.json();
    const templates = Array.isArray(data) ? data : (data.templates || data.items || []);
    console.log('[TEMPLATE] refresh list, count:', templates.length);
    const list = document.getElementById('template-list');
    if (!list) return;
    const search = (document.getElementById('template-search')?.value || '').toLowerCase();
    list.innerHTML = '';
    for (const t of templates) {
        if (search && !t.name.toLowerCase().includes(search) && !(t.description||'').toLowerCase().includes(search)) continue;
        const div = document.createElement('div');
        div.className = 'template-item';
        div.onclick = () => executeTemplateById(t.id);
        const tagsHtml = (t.tags||[]).map(tag => `<span class="template-item-tag">${tag}</span>`).join('');
        const isMulti = t.type.includes(',');
        const typeLabel = t.type;
        const followUpIcon = t.follow_up ? ' 🤖' : '';
        let extraInfo = '';
        if (t.type === 'qa' || (isMulti && t.type.split(',').includes('qa'))) {
          extraInfo = `<div style="font-size:11px;color:var(--text2)">Q: ${t.question || ''}</div>`;
        }
        div.innerHTML = `
          <div class="template-item-info">
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <span class="template-item-name">${t.name}</span>
              <span class="template-item-type">${typeLabel}${followUpIcon}</span>
            </div>
            ${extraInfo}
            ${(t.commands && typeof t.commands === 'object') ? Object.entries(t.commands).map(([ct, cmd]) =>
              `<div style="font-size:10px;color:var(--text-dim);margin-top:2px;font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"><b>${ct}:</b> ${cmd.replace(/\n/g, '↵ ').substring(0, 60)}</div>`
            ).join('') : (t.command ? `<div style="font-size:10px;color:var(--text-dim);margin-top:2px;font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${t.command.replace(/\n/g, '↵ ').substring(0, 80)}</div>` : '')}
            ${t.description ? `<div class="template-item-desc">${t.description}</div>` : ''}
            ${tagsHtml ? `<div class="template-item-tags">${tagsHtml}</div>` : ''}
          </div>
          <div style="display:flex;flex-direction:column;gap:4px;flex-shrink:0">
            <button class="template-item-del" title="Редактировать" onclick="event.stopPropagation();editTemplate('${t.id}')" style="font-size:12px;padding:2px 8px">✎</button>
            <button class="template-item-del" title="Удалить" onclick="event.stopPropagation();deleteTemplate('${t.id}')">✕</button>
          </div>`;
        list.appendChild(div);
      }
    } catch(e) {
      console.warn('[TEMPLATE] list error:', e);
    }
  };

  window.onTemplateTypeChange = function() {
    const sel = document.getElementById('template-type');
    const types = Array.from(sel.selectedOptions).map(o => o.value);
    const isQa = types.includes('qa');
    const nonQaTypes = types.filter(t => t !== 'qa');
    console.log('[TEMPLATE] type change:', types, 'isQa:', isQa);
    document.getElementById('template-question-row').classList.toggle('hidden', !isQa);
    document.getElementById('template-answers-row').classList.toggle('hidden', !isQa);
    // Show per-type code fields
    const container = document.getElementById('template-code-rows');
    container.innerHTML = '';
    const currentCodes = window._templateCodes || {};
    for (const ct of nonQaTypes) {
      const row = document.createElement('div');
      row.className = 'row';
      row.style.cssText = 'gap:8px';
      const label = document.createElement('div');
      label.className = 'text label';
      label.style.cssText = 'width:70px;font-size:11px';
      label.textContent = ct;
      const textarea = document.createElement('textarea');
      textarea.id = 'template-code-' + ct;
      textarea.rows = 3;
      textarea.placeholder = 'Код для ' + ct + '...';
      textarea.value = currentCodes[ct] || '';
      textarea.style.cssText = 'flex:1;padding:7px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px;font-family:monospace;resize:vertical';
      row.appendChild(label);
      row.appendChild(textarea);
      container.appendChild(row);
    }
  };

  window.showCreateTemplate = function() {
    document.getElementById('template-create-form').classList.remove('hidden');
    window._templateCodes = {};
    onTemplateTypeChange();
  };

  window.hideCreateTemplate = function() {
    document.getElementById('template-create-form').classList.add('hidden');
    document.getElementById('template-code-rows').innerHTML = '';
    window._editingTemplateId = null;
    const form = document.getElementById('template-create-form');
    const titleEl = form.querySelector('.template-form-title');
    if (titleEl) titleEl.textContent = 'Новый шаблон';
    const btn = form.querySelector('button[onclick="createTemplate()"]');
    if (btn) btn.textContent = 'Сохранить';
  };

  window._editingTemplateId = null;

  window.editTemplate = async function(id) {
    console.log('[TEMPLATE] editTemplate:', id);
    try {
      const resp = await fetch('/api/templates/get?id=' + encodeURIComponent(id));
      const data = await resp.json();
      if (!resp.ok) { alert('Ошибка: ' + (data.error || 'Не удалось загрузить шаблон')); return; }
      const t = data.template || data;
      if (!t) { alert('Шаблон не найден'); return; }
      window._editingTemplateId = String(t.id);
      // Заполняем форму
      document.getElementById('template-name').value = t.name || '';
      document.getElementById('template-desc').value = t.description || '';
      document.getElementById('template-tags').value = (t.tags || []).join(', ');
      document.getElementById('template-followup').checked = !!t.follow_up;
      const types = (t.type || '').split(',').map(s => s.trim()).filter(Boolean);
      const sel = document.getElementById('template-type');
      Array.from(sel.options).forEach(o => { o.selected = types.includes(o.value); });
      // Код/вопрос/ответы
      if (types.includes('qa')) {
        document.getElementById('template-question').value = t.question || '';
        document.getElementById('template-answers').value = (t.answers || []).join('\n');
      }
      document.getElementById('template-vars').value = t.variables && Object.keys(t.variables).length ? JSON.stringify(t.variables) : '';
      // Запоминаем текущие коды для onTemplateTypeChange
      window._templateCodes = {};
      if (t.commands && typeof t.commands === 'object') {
        Object.assign(window._templateCodes, t.commands);
      } else if (t.command) {
        // Вытаскиваем секции из объединённой команды
        const sections = t.command.split(/(?=^#--\w+--$)/m);
        for (const sec of sections) {
          const m = sec.match(/^#--(\w+)--\n?([\s\S]*)$/);
          if (m) window._templateCodes[m[1]] = m[2].trim();
        }
      }
      onTemplateTypeChange();
      // Меняем заголовок/кнопку
      const form = document.getElementById('template-create-form');
      form.classList.remove('hidden');
      const titleEl = form.querySelector('.template-form-title');
      if (titleEl) titleEl.textContent = 'Редактирование шаблона';
      const btn = form.querySelector('button[onclick="createTemplate()"]');
      if (btn) btn.textContent = 'Сохранить изменения';
    } catch(e) {
      console.error('[TEMPLATE] edit error:', e);
      alert('Ошибка загрузки: ' + e.message);
    }
  };

  window.createTemplate = async function() {
    const name = document.getElementById('template-name').value.trim();
    const sel = document.getElementById('template-type');
    const types = Array.from(sel.selectedOptions).map(o => o.value);
    const type = types.join(',');
    const description = document.getElementById('template-desc').value.trim();
    const tagsStr = document.getElementById('template-tags').value.trim();
    const question = document.getElementById('template-question').value.trim();
    const answersStr = document.getElementById('template-answers').value.trim();
    const varsStr = document.getElementById('template-vars').value.trim();
    const followUp = document.getElementById('template-followup').checked;
    if (!name) { alert('Введите название шаблона'); return; }
    if (!types.length) { alert('Выберите хотя бы один тип'); return; }
    const isQa = types.includes('qa');
    // Collect per-type codes
    const commands = {};
    const nonQaTypes = types.filter(t => t !== 'qa');
    let hasAnyCode = false;
    for (const ct of nonQaTypes) {
      const ta = document.getElementById('template-code-' + ct);
      if (ta) {
        const code = ta.value.trim();
        if (code) {
          commands[ct] = code;
          hasAnyCode = true;
        }
      }
    }
    if (!isQa && !hasAnyCode) { alert('Введите код для выбранных типов'); return; }
    if (isQa && !question) { alert('Введите вопрос для QA шаблона'); return; }
    let variables = {};
    if (varsStr) {
      try { variables = JSON.parse(varsStr); } catch(e) { console.error('[TEMPLATE] vars parse error:', e); alert('Неверный JSON в переменных: ' + e.message); return; }
    }
    const isEdit = !!window._editingTemplateId;
    console.log('[TEMPLATE]', isEdit ? 'updating' : 'creating', ':', {id: window._editingTemplateId, name, type, isQa, followUp, commands});
    try {
      const body = {name, type, description, tags: tagsStr.split(',').map(s=>s.trim()).filter(s=>s), follow_up: followUp};
      if (isEdit) body.id = window._editingTemplateId;
      if (isQa) {
        body.question = question;
        body.answers = answersStr.split('\n').map(s=>s.trim()).filter(s=>s);
        body.variables = variables;
      } else if (hasAnyCode) {
        body.commands = commands;
        body.command = Object.entries(commands).map(([ct, cmd]) => `#--${ct}--\n${cmd}`).join('\n');
      }
      const endpoint = isEdit ? '/api/templates/update' : '/api/templates/create';
      const resp = await fetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      const data = await resp.json();
      // Сервер возвращает {ok: true, template: {id, name, ...}}. Проверяем оба варианта
      // на случай, если в каком-то ответе появится top-level id.
      const createdId = (data && (data.id || (data.template && data.template.id)));
      if (resp.ok && createdId) {
        console.log('[TEMPLATE]', isEdit ? 'updated' : 'created', ':', createdId);
        hideCreateTemplate();
        document.getElementById('template-name').value = '';
        document.getElementById('template-desc').value = '';
        document.getElementById('template-tags').value = '';
        document.getElementById('template-vars').value = '';
        document.getElementById('template-question').value = '';
        document.getElementById('template-answers').value = '';
        document.getElementById('template-followup').checked = false;
        window._editingTemplateId = null;
        refreshTemplateList();
      } else {
        alert('Ошибка: ' + (data.error || 'Неизвестная ошибка'));
      }
    } catch(e) {
      console.error('[TEMPLATE] save error:', e);
      alert('Ошибка сети: ' + e.message);
    }
  };

  window.filterTemplateList = function() {
    refreshTemplateList();
  };

  window.deleteTemplate = async function(id) {
    if (!confirm('Удалить этот шаблон?')) return;
    console.log('[TEMPLATE] deleting:', id);
    await fetch('/api/templates/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})
    });
    refreshTemplateList();
  };

  // ── Execute template and optionally forward output to AI ──
  window._pendingFollowUp = null;

  // Сервер оборачивает результат в {ok, result} — распаковываем здесь,
  // чтобы остальной код мог читать success/output/follow_up/animations напрямую.
  function unwrapTemplateResult(data) {
    if (data && typeof data === 'object' && data.result && typeof data.result === 'object') {
      return data.result;
    }
    return data || {};
  }

  window.executeTemplateById = async function(id, args) {
    console.log('[TEMPLATE] executeById:', id, 'args:', args);
    try {
      const resp = await fetch('/api/templates/execute', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id, args: args || ''})
      });
      const data = await resp.json();
      const result = unwrapTemplateResult(data);
      console.log('[TEMPLATE] execute result:', result);
      playTemplateAnimations(result);
      if (result.follow_up && result.success && result.output) {
        // Forward to AI for commentary
        window._pendingFollowUp = result.output;
        const aiInput = `Я выполнил команду и получил результат:\n\`\`\`\n${result.output}\n\`\`\`\nПрокомментируй этот результат.`;
        const nodeInput = addMessage(true);
        nodeInput.dataset.input = aiInput;
        addText(nodeInput, aiInput);
        // Trigger AI send
        const input = document.getElementById('input');
        if (input) {
          input.value = '';
          triggerAISend();
        }
        return;
      }
      if (result.success) {
        alert('Результат:\n' + (result.output || '(пусто)'));
      } else {
        alert('Ошибка:\n' + (result.output || 'Выполнение не удалось'));
      }
    } catch(e) {
      console.error('[TEMPLATE] executeById error:', e);
      alert('Ошибка сети: ' + e.message);
    }
  };

  // ── Chat template matching: trigger before AI send ──
  // This is called from the keydown handler (see _checkTemplateMatch)
  // ── Helper: play template animations ──
  function playTemplateAnimations(exResult) {
    if (exResult.animations && exResult.animations.length > 0 && window.playMotion) {
      console.log('[TEMPLATE] playing animations:', exResult.animations);
      for (const anim of exResult.animations) {
        setTimeout(() => window.playMotion(anim), 100);
      }
    }
  }

  window._checkTemplateMatch = async function(text) {
    console.log('[TEMPLATE] _checkTemplateMatch:', text);
    try {
      const resp = await fetch('/api/templates/match?text=' + encodeURIComponent(text));
      const data = await resp.json();
      if (data.match && data.match.id) {
        console.log('[TEMPLATE] match found:', data.match.name, 'id:', data.match.id);
        const exResp = await fetch('/api/templates/execute', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: data.match.id, args: text})
        });
        const exData = await exResp.json();
        const exResult = unwrapTemplateResult(exData);
        console.log('[TEMPLATE] matched execute result:', exResult);
        const nodeOutput = addMessage(false);
        playTemplateAnimations(exResult);
        if (exResult.follow_up && exResult.success) {
          addText(nodeOutput, exResult.output);
          window._pendingFollowUp = exResult.output;
          const aiInput = `Я выполнил команду и получил результат:\n\`\`\`\n${exResult.output}\n\`\`\`\nПрокомментируй этот результат.`;
          const nodeInput = addMessage(true);
          nodeInput.dataset.input = aiInput;
          addText(nodeInput, aiInput);
          const input = document.getElementById('input');
          if (input) { input.value = aiInput; triggerAISend(); }
          return true;
        }
        addText(nodeOutput, exResult.success ? (exResult.output || '✅ Выполнено') : ('❌ ' + (exResult.output || 'Неизвестная ошибка')));
        if (!exResult.animations && window.playMotion) window.playMotion('nod_yes');
        return true;
      } else if (data.suggestions && data.suggestions.length > 0) {
        console.log('[TEMPLATE] no match, suggestions:', data.suggestions);
        window._pendingSuggestions = data.suggestions;
      } else {
        window._pendingSuggestions = null;
      }
    } catch(e) {
      console.error('[TEMPLATE] _checkTemplateMatch error:', e);
    }
    return false;
  };

  // ── Show suggestions UI ──
  window.showSuggestions = function(suggestions) {
    console.log('[TEMPLATE] showSuggestions:', suggestions.length);
    let container = document.getElementById('suggestions-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'suggestions-container';
      container.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px;padding:4px 8px;border-top:1px solid var(--border)';
      const inputRow = document.getElementById('input')?.closest('.row') || document.querySelector('.chat-tools-row');
      if (inputRow && inputRow.parentNode) {
        inputRow.parentNode.insertBefore(container, inputRow.nextSibling);
      }
    }
    container.innerHTML = suggestions.map(s =>
      `<button class="button" style="font-size:11px;padding:3px 10px;border-radius:12px;background:var(--accent-soft);border-color:var(--accent);color:var(--accent)" onclick="useSuggestion('${s.id}')">${s.question}</button>`
    ).join('');
  };

  window.useSuggestion = async function(id) {
    console.log('[TEMPLATE] useSuggestion:', id);
    const nodeInput = addMessage(true);
    const sug = (window._pendingSuggestions || []).find(s => s.id === id);
    const text = sug ? sug.question : '';
    if (!text) return;
    nodeInput.dataset.input = text;
    addText(nodeInput, text);
    const exResp = await fetch('/api/templates/execute', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, args: text})
    });
    const exData = await exResp.json();
    const exResult = unwrapTemplateResult(exData);
    console.log('[TEMPLATE] suggestion execute result:', exResult);
    const nodeOutput = addMessage(false);
    playTemplateAnimations(exResult);
    if (exResult.follow_up && exResult.success) {
      addText(nodeOutput, exResult.output);
      window._pendingFollowUp = exResult.output;
      const aiInput = `Я выполнил команду и получил результат:\n\`\`\`\n${exResult.output}\n\`\`\`\nПрокомментируй этот результат.`;
      const nodeInput2 = addMessage(true);
      nodeInput2.dataset.input = aiInput;
      addText(nodeInput2, aiInput);
      const input = document.getElementById('input');
      if (input) { input.value = ''; triggerAISend(); }
      window._pendingSuggestions = null;
      return;
    }
    addText(nodeOutput, exResult.success ? (exResult.output || '✅ Выполнено') : ('❌ ' + (exResult.output || 'Неизвестная ошибка')));
    const container = document.getElementById('suggestions-container');
    if (container) container.innerHTML = '';
    window._pendingSuggestions = null;
    if (!exResult.animations && window.playMotion) window.playMotion('nod_yes');
  };

  // Auto-refresh template list when templates page is shown
  document.addEventListener('visibilitychange', () => {
    const el = document.getElementById('right-templates');
    if (el && !el.classList.contains('hidden')) refreshTemplateList();
  });

  // ── Helper: trigger AI send from current input ──
  function triggerAISend() {
    const input = document.getElementById('input');
    if (!input) return;
    const ev = new KeyboardEvent('keydown', {key:'Enter', ctrlKey:false, shiftKey:false, bubbles:true});
    input.dispatchEvent(ev);
  }

});
