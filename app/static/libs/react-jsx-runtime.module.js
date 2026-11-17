import R from "/static/libs/react.module.js";
function jsx(type, props, key) {
  const config = {};
  if (key != null) config.key = '' + key;
  if (props != null) {
    for (const k in props) {
      if (k !== 'key' && k !== 'ref') config[k] = props[k];
    }
    if (props.ref != null) config.ref = props.ref;
  }
  if (type && type.defaultProps) {
    for (const k in type.defaultProps) {
      if (config[k] === void 0) config[k] = type.defaultProps[k];
    }
  }
  return R.createElement(type, config);
}
export const Fragment = R.Fragment;
export { jsx, jsx as jsxs, jsx as jsxDEV };
