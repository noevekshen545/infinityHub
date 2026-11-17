var hasOwn = {}.hasOwnProperty;
function parseValue(arg) {
  if (typeof arg === 'string' || typeof arg === 'number') return arg;
  if (typeof arg !== 'object') return '';
  if (Array.isArray(arg)) {
    var classes = '';
    for (var i = 0; i < arg.length; i++) {
      var inner = parseValue(arg[i]);
      if (inner) classes = classes ? classes + ' ' + inner : inner;
    }
    return classes;
  }
  var classes = '';
  for (var key in arg) {
    if (hasOwn.call(arg, key) && arg[key]) classes = classes ? classes + ' ' + key : key;
  }
  return classes;
}
export default function classNames() {
  var classes = '';
  for (var i = 0; i < arguments.length; i++) {
    var arg = arguments[i];
    if (arg) classes = classes ? classes + ' ' + parseValue(arg) : parseValue(arg);
  }
  return classes;
}
