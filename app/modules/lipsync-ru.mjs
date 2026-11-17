
class LipsyncRu {

  constructor() {
    this.visemes = {
      'а': 'aa', 'я': 'aa', 'е': 'E', 'э': 'E', 'ё': 'O',
      'и': 'I', 'ы': 'I', 'й': 'I', 'о': 'O', 'у': 'U', 'ю': 'U',
      'б': 'PP', 'п': 'PP', 'м': 'PP',
      'в': 'FF', 'ф': 'FF',
      'д': 'DD', 'т': 'DD',
      'г': 'kk', 'к': 'kk', 'х': 'kk',
      'л': 'nn', 'р': 'RR',
      'с': 'SS', 'з': 'SS', 'ц': 'SS',
      'ш': 'CH', 'щ': 'CH', 'ж': 'CH', 'ч': 'CH',
      'ь': 'sil', 'ъ': 'sil'
    };

    this.durations = {
      'а': 0.95, 'я': 0.95, 'е': 0.90, 'э': 0.90, 'ё': 0.96,
      'и': 0.92, 'ы': 0.92, 'й': 0.50, 'о': 0.96, 'у': 0.95, 'ю': 0.95,
      'б': 1.08, 'п': 1.08, 'м': 1.08,
      'в': 1.00, 'ф': 1.00,
      'д': 1.05, 'т': 1.05,
      'г': 1.21, 'к': 1.21, 'х': 1.21,
      'л': 0.88, 'р': 0.88,
      'с': 1.23, 'з': 1.23, 'ц': 1.23,
      'ш': 1.23, 'щ': 1.45, 'ж': 1.23, 'ч': 1.15,
      'ь': 0.3, 'ъ': 0.3
    };

    this.pauses = { ' ': 1, ',': 3, '-': 0.5, '—': 1.5, '…': 2 };

    this.numbers = [
      "ноль", "один", "два", "три", "четыре", "пять", "шесть",
      "семь", "восемь", "девять", "десять", "одиннадцать", "двенадцать",
      "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
      "семнадцать", "восемнадцать", "девятнадцать"
    ];
    this.tens = [
      this.numbers[0], this.numbers[10], "двадцать",
      "тридцать", "сорок", "пятьдесят", "шестьдесят",
      "семьдесят", "восемьдесят", "девяносто"
    ];
  }

  numberToRussianWords(x) {
    const w = [];
    let n = parseFloat(x);
    if (isNaN(n)) return x;

    let p = (n, z, w1, w2, w3) => {
      if (n < z) return n;
      const d = Math.floor(n / z);
      if (d === 1) {
        w.push(this.numbers[1]);
      } else {
        w.push(this.numberToRussianWords(d.toString()));
      }
      if (d % 10 === 1 && d % 100 !== 11) {
        w.push(w1);
      } else if ((d % 10 >= 2 && d % 10 <= 4) && (d % 100 < 10 || d % 100 >= 20)) {
        w.push(w2);
      } else {
        w.push(w3);
      }
      return n - d * z;
    };

    if (n < 0) {
      w.push('минус');
      n = Math.abs(n);
    }

    n = p(n, 1000000000, "миллиард", "миллиарда", "миллиардов");
    n = p(n, 1000000, "миллион", "миллиона", "миллионов");
    n = p(n, 1000, "тысяча", "тысячи", "тысяч");
    n = p(n, 100, "сто", "ста", "сот");

    for (let i = this.tens.length - 1; i >= 1; i--) {
      if (n >= 10 * i) {
        w.push(this.tens[i]);
        n -= 10 * i;
        break;
      }
    }
    if (n >= 1) {
      let d = Math.floor(n);
      w.push(this.numbers[d]);
      n -= d;
    }
    if (n >= 0 && Math.abs(parseFloat(x)) < 1) w.push(this.numbers[0]);
    if (n > 0) {
      let d = x.split('.');
      if (d.length > 1) {
        w.push('целых');
        let c = [...d[d.length - 1]];
        for (let i = 0; i < c.length; i++) {
          w.push(this.numbers[c[i]]);
        }
      }
    }
    return w.join(' ').trim();
  }

  preProcessText(s) {
    return s
      .replace(/[#_*'":;]/g, '')
      .replace(/(\d)\.(\d)/g, '$1 целых $2')
      .replace(/\d+/g, this.numberToRussianWords.bind(this))
      .replace(/(\D)\1\1+/g, '$1$1')
      .replace(/  +/g, ' ')
      .trim();
  }

  wordsToVisemes(w) {
    let o = { words: w, visemes: [], times: [], durations: [] };
    let t = 0;

    const chars = [...w];
    for (let i = 0; i < chars.length; i++) {
      const c = chars[i].toLowerCase();
      const viseme = this.visemes[c];
      if (viseme) {
        if (o.visemes.length && o.visemes[o.visemes.length - 1] === viseme) {
          const d = 0.7 * (this.durations[c] || 1);
          o.durations[o.durations.length - 1] += d;
          t += d;
        } else {
          const d = this.durations[c] || 1;
          o.visemes.push(viseme);
          o.times.push(t);
          o.durations.push(d);
          t += d;
        }
      } else {
        t += this.pauses[chars[i]] || 0;
      }
    }

    return o;
  }

}

export { LipsyncRu };
