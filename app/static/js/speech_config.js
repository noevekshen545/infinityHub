/**
 * speech_config.js — центральная конфигурация распознавания и синтеза речи.
 *
 * Настройки задаются на вкладке «Распознавание и синтез речи» (page_settings.html)
 * и сохраняются в localStorage под ключами speech_*.
 * Страницы используют глобальный дефолт из настроек, но могут переопределить
 * движок локально (localStorage[prefix+'ttsEngine'] и т.п.).
 */
window.SpeechConfig = (function () {
  function get(key, fallback) {
    var v = localStorage.getItem(key);
    return (v === null || v === undefined || v === '') ? fallback : v;
  }
  function num(key, fallback) {
    var v = parseFloat(localStorage.getItem(key));
    return isNaN(v) ? fallback : v;
  }
  return {
    ttsEngine: function () { return get('speech_tts_engine', 'auto'); },
    ttsSpeaker: function () { return get('speech_tts_speaker', 'xenia'); },
    ttsVoice: function () { return get('speech_tts_voice', ''); },
    ttsRate: function () { return num('speech_tts_rate', 1.0); },
    ttsPitch: function () { return num('speech_tts_pitch', 0); },
    sttEngine: function () { return get('speech_stt_engine', 'local'); },
    selectionTtsEnabled: function () { return get('speech_selection_tts', '1') !== '0'; },
    voiceInputEnabled: function () { return get('speech_voice_input', '1') !== '0'; },

    // Настройки для конкретной страницы: локальный оверрайд страницы (prefix)
    // имеет приоритет над глобальными настройками.
    pageTts: function (prefix) {
      prefix = prefix || '';
      return {
        engine: get(prefix + 'ttsEngine', this.ttsEngine()),
        speaker: get(prefix + 'ttsSpeaker', this.ttsSpeaker()),
        voice: get(prefix + 'ttsVoice', this.ttsVoice())
      };
    },
    // Заполняет дефолты в объект состояния страницы ({ttsEngine, ttsSpeaker, ttsVoice})
    applyPageDefaults: function (stateObj, prefix) {
      var p = this.pageTts(prefix);
      if (stateObj) {
        stateObj.ttsEngine = p.engine;
        stateObj.ttsSpeaker = p.speaker;
        stateObj.ttsVoice = p.voice;
      }
      return p;
    }
  };
})();
