// Site configuration
export const site = {

  // Preset avatars
  avatars: {
    'Brunette': {
      url: './avatars/brunette.glb',
      body: 'F',
      avatarMood: 'neutral',
      fi: 'Brunetti',
      ru: 'Брюнетка'
    },

    'MPFB': {
      url: './avatars/mpfb.glb',
      body: 'F',
      avatarMood: 'happy',
      baseline: {},
      modelDynamicBones: [
        {
          "bone":"LeftBreast",
          "type":"point",
          "stiffness":600,
          "damping":20,
          "deltaLocal":[0,0.01,0],
          "deltaWorld":[0,-0.02,0],
          "limits":[[-0.02,0.02],[-0.02,0.06],[-0.02,0.02],[-0.5,0.5]]
        },
        {
          "bone":"RightBreast",
          "type":"point",
          "stiffness":600,
          "damping":20,
          "deltaLocal":[0,0.01,0],
          "deltaWorld":[0,-0.02,0],
          "limits":[[-0.02,0.02],[-0.02,0.06],[-0.02,0.02],[-0.5,0.5]]
        },
        {
          "bone":"Ponytail1",
          "type":"mix2",
          "stiffness":100,
          "damping":4,
          "limits":[null,null,[null,0.02],null],
          "pivot":true
        },
        {
          "bone":"Ponytail2",
          "type":"mix2",
          "stiffness":150,
          "damping":4,
          "limits":[null,null,[null,0.01],null]
        },
        {
          "bone":"Ponytail3",
          "type":"mix2",
          "stiffness":200,
          "damping":4,
          "limits":[null,null,[null,0.01],null]
        }
      ],
      baseline: {
        headRotateX: -0.01,
        eyeBlinkLeft: 0.05,
        eyeBlinkRight: 0.05
      }
    },

    'Avaturn': {
      url: './avatars/avaturn.glb',
      body: 'F',
      avatarMood: 'happy',
      retarget: {
        Hips: { y: 0.03 }, Spine: { y: 0.02 }, Spine1: { y: 0.02, z: 0.01 },
        Spine2: { y: 0.02, z: 0.01 }, Neck: { z: 0.02, y: 0.01 }, Head: { z: 0.02 },
        LeftShoulder: { rx: -0.5 }, RightShoulder: { rx: -0.5 },
        scaleToHipsLevel: 1.0
      },
      baseline: {
        headRotateX: -0.05,
        eyeBlinkLeft: 0.15,
        eyeBlinkRight: 0.15
      }
    },

    'AvatarSDK': {
      url: './avatars/avatarsdk.glb',
      body: 'M',
      avatarMood: 'neutral',
      retarget: {
        Neck: { z: -0.01, rx: -0.15 }, Neck1: { z: -0.01, rx: -0.15 }, Neck2: { z: -0.01, rx: -0.15 },
        LeftShoulder: { rz: -0.3 }, RightShoulder: { rz: 0.3 },
        scaleToEyesLevel: 1.0, origin: { y: -0.1 }
      },
      baseline: {
        headRotateX: -0.04,
        eyeBlinkLeft: 0.05,
        eyeBlinkRight: 0.05
      }
    },

    'VRoid': {
      url: './avatars/vroid.glb',
      body: 'F',
      avatarMood: 'neutral',
      baseline: {
        headRotateX: -0.1,
        eyeBlinkLeft: 0.05,
        eyeBlinkRight: 0.05
      },
      modelDynamicBones: [
        {
          "bone":"J_Sec_L_Bust1",
          "type":"point",
          "stiffness":400,
          "damping":10,
          "deltaLocal":[0,0.01,0],
          "deltaWorld":[0,-0.02,0],
          "limits":[[-0.01,0.01],[-0.01,0.01],[-0.01,0.01],[-0.01,0.01]]
        },
        {
          "bone":"J_Sec_R_Bust1",
          "type":"point",
          "stiffness":400,
          "damping":10,
          "deltaLocal":[0,0.01,0],
          "deltaWorld":[0,-0.02,0],
          "limits":[[-0.01,0.01],[-0.01,0.01],[-0.01,0.01],[-0.01,0.01]]
        },
        {"bone":"J_Sec_Hair2_01","type":"mix2","stiffness":100,"damping":4,"limits":[null,null,[null,0.01],null]},
        {"bone":"J_Sec_Hair2_02","type":"mix2","stiffness":100,"damping":4,"limits":[null,null,[null,0.01],null]},
        {"bone":"J_Sec_Hair2_03","type":"mix2","stiffness":100,"damping":4,"limits":[null,null,[null,0.01],null]},
        {"bone":"J_Sec_Hair2_04","type":"mix2","stiffness":100,"damping":4,"limits":[null,null,[null,0.01],null]},
        {"bone":"J_Sec_Hair2_05","type":"mix2","stiffness":100,"damping":4,"limits":[null,null,[null,0.01],null]},
        {"bone":"J_Sec_Hair2_06","type":"mix2","stiffness":100,"damping":4,"limits":[null,null,[null,0.01],null]}
      ],
      fi: 'VRoid',
      ru: 'VRoid'
    },

    'InfinityVRM': {
      url: './avatars/infinity.vrm',
      body: 'F',
      avatarMood: 'neutral',
      fi: 'Infinity VRM',
      ru: 'Infinity VRM'
    }

  },

  // Google voices
  googleVoices: {
    "fi-F": { id: "fi-FI-Standard-A" },
    "lv-M": { id: "lv-LV-Standard-A" },
    "lt-M": { id: "lt-LT-Standard-A" },
    "en-F": { id: "en-GB-Standard-A" },
    "en-M": { id: "en-GB-Standard-D" },
    "fr-F": { id: "fr-FR-Standard-A" },
    "fr-M": { id: "fr-FR-Standard-B" },
    "de-F": { id: "de-DE-Standard-G" },
    "de-M": { id: "de-DE-Standard-H" },
  },

  // ElevenLab voices
  elevenVoices: {
    "Bella": { id: "EXAVITQu4vr4xnSDxMaL" },
    "Elli": { id: "MF3mGyEYCl7XYWbV9V6O" },
    "Rachel": { id: "21m00Tcm4TlvDq8ikWAM" },
    "Adam": { id: "pNInz6obpgDQGcFmaJgB" },
    "Antoni": { id: "ErXwobaYiN019PkySvjV" },
    "Arnold": { id: "VR6AewLTigWG4xSOukaG" },
    "Domi": { id: "AZnzlk1XvdvUeBnXmlld" },
    "Josh": { id: "TxGEqnHWrfWFTfGW9XjX" },
    "Sam": { id: "yoZ06aMxZJJ28mfd3POQ" }
  },

  // Microsoft voices
  microsoftVoices: {
    "fi-Selma": { lang: "fi-FI", id: "fi-FI-SelmaNeural" },
    "fi-Noora": { lang: "fi-FI", id: "fi-FI-NooraNeural" },
    "fi-Harri": { lang: "fi-FI", id: "fi-FI-HarriNeural" },
    "en-Jenny": { lang: "en-US", id: "en-US-JennyNeural" },
    "en-Tony": { lang: "en-US", id: "en-US-TonyNeural" },
    "fr-Vivienne": { lang: "fr-FR", id: "fr-FR-VivienneMultilingualNeural" },
    "fr-Lucien": { lang: "fr-FR", id: "fr-FR-LucienMultilingualNeural" },
    "de-Seraphina": { lang: "de-DE", id: "de-DE-SeraphinaMultilingualNeural" },
    "de-Florian": { lang: "de-DE", id: "de-DE-FlorianMultilingualNeural" },
  },

  // Silero TTS voices (local)
  sileroVoices: {
    "ru-Xenia": { speaker: "xenia", lang: "ru", fi: 'Xenia (Venäjä)', ru: 'Ксения (Русский)' },
    "ru-Baya": { speaker: "baya", lang: "ru", fi: 'Baya (Venäjä)', ru: 'Бая (Русский)' },
    "ru-Kristina": { speaker: "kristina", lang: "ru", fi: 'Kristina (Venäjä)', ru: 'Кристина (Русский)' },
    "ru-Aidar": { speaker: "aidar", lang: "ru", fi: 'Aidar (Venäjä)', ru: 'Аидар (Русский)' },
    "ru-Denis": { speaker: "denis", lang: "ru", fi: 'Denis (Venäjä)', ru: 'Денис (Русский)' },
  },

  // Preset views
  views: {
    'DrStrange': { url: './views/strange.jpg', type: 'image/jpg', fi: 'TohtoriOuto',
      ru: 'Доктор Стрэндж' },
    'Matrix': { url: './views/matrix.mp4', type: 'video/mp4' }
  },

  // Preset poses (includes internal poses)
  poses: {
    'Straight': { url: "straight", fi: 'Suora',
      ru: 'Прямо' },
    'Side': { url: "side", fi: 'Keno',
      ru: 'Боком' },
    'Hip': { url: "hip", fi: 'Lantio',
      ru: 'Бедро' },
    'Turn': { url: "turn", fi: 'Sivu',
      ru: 'Поворот' },
    'Back': { url: "back", fi: 'Taka',
      ru: 'Сзади' },
    'Wide': { url: "wide", fi: 'Haara',
      ru: 'Широко' },
    'OneKnee': { url: "oneknee", fi: 'Polvi',
      ru: 'Одно колено' },
    'TwoKnees': { url: "kneel", fi: 'Polvet',
      ru: 'Колени' },
    'Bend': { url: "bend", fi: 'Perä',
      ru: 'Наклон' },
    'Sitting': { url: "sitting", fi: 'Istuva',
      ru: 'Сидя' },
    'Dance': { url: './poses/dance.fbx', fi: 'Tanssi',
      ru: 'Танец' }
  },

  // Preset gestures
  gestures: {
    'HandUp': { name: "handup", fi: 'KäsiYlös',
      ru: 'Рука вверх' },
    'OK': { name: "ok" },
    'Index': { name: "index", fi: 'Etusormi',
      ru: 'Указательный' },
    'ThumbUp': { name: "thumbup", fi: 'PeukaloYlös',
      ru: 'Большой вверх' },
    'ThumbDown': { name: "thumbdown", fi: 'PeukaloAlas',
      ru: 'Большой вниз' },
    'Side': { name: "side", fi: 'Sivu', ru: 'В сторону' },
    'Shrug': { name: "shrug", fi: 'Olankohautus',
      ru: 'Пожатие плечами' },
    'Namaste': { name: "namaste" }
  },

  // Preset animations
  animations: {
    'Walking': { url: './animations/walking.fbx', fi: 'Kävely',
      ru: 'Ходьба' }
  },

  // Impulse responses
  impulses: {
    'Room': { url: './audio/ir-room.m4a', fi: 'Huone',
      ru: 'Комната' },
    'Basement': { url: './audio/ir-basement.m4a', fi: 'Kellari',
      ru: 'Подвал' },
    'Forest': { url: './audio/ir-forest.m4a', fi: 'Metsä',
      ru: 'Лес' },
    'Church': { url: './audio/ir-church.m4a', fi: 'Kirkko',
      ru: 'Церковь' }
  },

  // Background ambient sounds/music
  music: {
    'Murmur': { url: './audio/murmur.mp3', fi: 'Puheensorina',
      ru: 'Гул'}
  }

};
