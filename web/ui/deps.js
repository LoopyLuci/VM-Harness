// Google Closure Library module — VM-Harness dashboard
// Compiled with ADVANCED optimizations

goog.provide('vmharness.ui.Dashboard');

goog.scope(function() {
    /** @constructor @extends {goog.ui.Component} */
    vmharness.ui.Dashboard = function() {
        goog.ui.Component.call(this);
    };
    goog.inherits(vmharness.ui.Dashboard, goog.ui.Component);

    /** @inheritDoc */
    vmharness.ui.Dashboard.prototype.createDom = function() {
        var dom = this.getDomHelper();
        var el = dom.createDom('div', 'vmharness-dashboard');
        this.setElementInternal(el);
    };
});
