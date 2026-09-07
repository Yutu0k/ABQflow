(function () {
  "use strict";

  function languageInfo(pathname) {
    var match = pathname.match(/\/(en|zh_CN)(?=\/|$)/);
    var current = match ? match[1] : "en";
    var target = current === "en" ? "zh_CN" : "en";
    var label = current === "en" ? "中文" : "English";
    var title = current === "en" ? "切换到中文" : "Switch to English";
    var targetPath;

    if (match) {
      targetPath = pathname.replace(/\/(en|zh_CN)(?=\/|$)/, "/" + target);
    } else {
      var sourceIndex = pathname.indexOf("/source/");
      var splitIndex = sourceIndex >= 0 ? sourceIndex : pathname.lastIndexOf("/");
      targetPath = pathname.slice(0, splitIndex) + "/" + target + pathname.slice(splitIndex);
    }

    return { label: label, title: title, targetPath: targetPath };
  }

  function addLanguageSwitcher() {
    var container = document.querySelector(".content-icon-container");
    var info = languageInfo(window.location.pathname);
    if (!container || !info || container.querySelector(".language-switcher")) {
      return;
    }

    var switcherContainer = document.createElement("div");
    switcherContainer.className = "language-switcher-container";

    var switcher = document.createElement("a");
    switcher.className = "language-switcher muted-link";
    switcher.href = info.targetPath + window.location.search + window.location.hash;
    switcher.title = info.title;
    switcher.setAttribute("aria-label", info.title);

    var svgNamespace = "http://www.w3.org/2000/svg";
    var icon = document.createElementNS(svgNamespace, "svg");
    icon.setAttribute("viewBox", "0 0 640 640");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");

    var path = document.createElementNS(svgNamespace, "path");
    path.setAttribute(
      "d",
      "M192 64C209.7 64 224 78.3 224 96L224 128L352 128C369.7 128 384 142.3 384 160C384 177.7 369.7 192 352 192L342.4 192L334 215.1C317.6 260.3 292.9 301.6 261.8 337.1C276 345.9 290.8 353.7 306.2 360.6L356.6 383L418.8 243C423.9 231.4 435.4 224 448 224C460.6 224 472.1 231.4 477.2 243L605.2 531C612.4 547.2 605.1 566.1 589 573.2C572.9 580.3 553.9 573.1 546.8 557L526.8 512L369.3 512L349.3 557C342.1 573.2 323.2 580.4 307.1 573.2C291 566 283.7 547.1 290.9 531L330.7 441.5L280.3 419.1C257.3 408.9 235.3 396.7 214.5 382.7C193.2 399.9 169.9 414.9 145 427.4L110.3 444.6C94.5 452.5 75.3 446.1 67.4 430.3C59.5 414.5 65.9 395.3 81.7 387.4L116.2 370.1C132.5 361.9 148 352.4 162.6 341.8C148.8 329.1 135.8 315.4 123.7 300.9L113.6 288.7C102.3 275.1 104.1 254.9 117.7 243.6C131.3 232.3 151.5 234.1 162.8 247.7L173 259.9C184.5 273.8 197.1 286.7 210.4 298.6C237.9 268.2 259.6 232.5 273.9 193.2L274.4 192L64.1 192C46.3 192 32 177.7 32 160C32 142.3 46.3 128 64 128L160 128L160 96C160 78.3 174.3 64 192 64zM448 334.8L397.7 448L498.3 448L448 334.8z"
    );
    icon.appendChild(path);
    switcher.appendChild(icon);
    switcherContainer.appendChild(switcher);

    var viewButton = container.querySelector(".view-this-page");
    if (viewButton) {
      viewButton.insertAdjacentElement("beforebegin", switcherContainer);
    } else {
      container.insertBefore(switcherContainer, container.firstChild);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", addLanguageSwitcher);
  } else {
    addLanguageSwitcher();
  }
})();
