/* Kartograf look for kepler.gl: the brkmyr.com palette and Inter Tight.
 *
 * Shared by web/index.html (served by server.py) and the pages kepler_view.py
 * builds for the Streamlit app. The palette is monochrome on warm paper, so the
 * data is drawn in ink and greys as well; the answer layer is the only solid ink.
 */
(function () {
  const C = {
    bg: "#eeeee8", paper: "#f6f6f1", panel: "#e6e6de", hover: "#dcdcd2",
    ink: "#171712", sec: "#5a5a52", mute: "#8a8a80", rule: "#d7d7cd"
  };
  const rgb = function (hex) {
    return [1, 3, 5].map(function (i) { return parseInt(hex.slice(i, i + 2), 16); });
  };
  const FONT = '"Inter Tight", "Helvetica Neue", Arial, sans-serif';

  function theme(K) {
    const base = (K && K.themeLT) || {};
    return Object.assign({}, base, {
      fontFamily: FONT,
      textColor: C.sec, textColorHl: C.ink, titleTextColor: C.ink,
      labelColor: C.sec, labelHoverColor: C.ink, subtextColor: C.mute, subtextColorActive: C.ink,
      activeColor: C.ink, activeColorHover: C.ink,
      sidePanelBg: C.bg, sidePanelHeaderBg: C.bg, sideBarCloseBtnBgd: C.bg,
      panelBackground: C.panel, panelBackgroundHover: C.hover, panelBorderColor: C.rule,
      panelHeaderIcon: C.sec, panelHeaderIconActive: C.ink,
      mapPanelBackgroundColor: C.bg, mapPanelHeaderBackgroundColor: C.panel,
      inputBgd: C.paper, inputBgdHover: C.bg, inputBgdActive: C.paper,
      inputBorderColor: C.rule, inputBorderHoverColor: C.mute, inputBorderActiveColor: C.ink,
      inputColor: C.ink, selectBackground: C.paper, selectBackgroundHover: C.bg, selectColor: C.ink,
      dropdownListBgd: C.paper, dropdownListHighlightBg: C.hover, dropdownListBorderTop: C.rule,
      primaryBtnBgd: C.ink, primaryBtnActBgd: "#000000", primaryBtnBgdHover: "#000000",
      primaryBtnColor: C.bg, primaryBtnActColor: C.bg,
      secondaryBtnBgd: C.sec, secondaryBtnActBgd: C.ink, secondaryBtnBgdHover: C.ink,
      floatingBtnBgd: C.bg, floatingBtnActBgd: C.panel, floatingBtnBgdHover: C.panel,
      floatingBtnColor: C.sec, floatingBtnActColor: C.ink,
      tooltipBg: C.ink, tooltipColor: C.bg
    });
  }

  // Road classes in alphabetical order, which is how kepler's ordinal scale
  // assigns colours: motorway and trunk dark, minor classes light.
  const ROAD_CLASSES = ["motorway", "primary", "secondary", "tertiary", "trunk"];
  const ROAD_GREYS = [C.ink, C.sec, "#8a8a80", "#b4b4aa", "#2f2f29"];

  function layer(d, answer) {
    const cols = d.columns || [];
    if (cols.indexOf("_geojson") >= 0) {
      const byClass = cols.indexOf("class") >= 0;
      return {
        id: d.id, type: "geojson",
        config: {
          dataId: d.id, label: d.label, color: rgb(answer ? C.ink : C.sec),
          columns: {geojson: "_geojson"}, isVisible: true,
          visConfig: {
            opacity: 0.9, strokeOpacity: 0.9, thickness: answer ? 2 : 0.6,
            strokeColor: rgb(answer ? C.ink : C.sec), stroked: true, filled: false,
            strokeColorRange: {name: "Kartograf roads", type: "custom", category: "Custom",
                               colors: byClass && !answer ? ROAD_GREYS : [C.ink]}
          }
        },
        visualChannels: byClass && !answer
          ? {strokeColorField: {name: "class", type: "string"}, strokeColorScale: "ordinal"}
          : {}
      };
    }
    return {
      id: d.id, type: "point",
      config: {
        dataId: d.id, label: d.label, color: rgb(C.ink),
        columns: {lat: "lat", lng: "lon"}, isVisible: true,
        visConfig: answer
          ? {radius: 7, opacity: 0.95, filled: true, outline: true, thickness: 2, strokeColor: rgb(C.bg)}
          : {radius: 1.6, opacity: 0.22, filled: true}
      }
    };
  }

  window.Kartograf = {COLORS: C, FONT: FONT, theme: theme, layer: layer, ROAD_CLASSES: ROAD_CLASSES};
})();
