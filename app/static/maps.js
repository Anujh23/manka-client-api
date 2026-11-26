/* Maps JavaScript API.
 *
 *   <img id="map" alt="Map">
 *   <script src="https://api.mankacommerceandconsultancy.co.in/v1/maps.js?key=YOUR_KEY"></script>
 *   <script>
 *     MapsAPI.locate().then((loc) => { document.getElementById('map').src = loc.image; });
 *   </script>
 *
 * MapsAPI.locate() -> Promise<{ id, request, lat, lng, accuracy_m, located_at, image, address }>
 *   Asks for location permission, waits for a good GPS fix and records it (charged). image is the URL of a PNG map of
 *   the spot (pin + accuracy circle, OpenStreetMap): use it in an <img>, or on a canvas with img.crossOrigin =
 *   'anonymous'. Show "© OpenStreetMap" next to it (licence), or add ?credit=1 to the URL for a small label on it.
 *   address is from OpenStreetMap, or null.
 * Errors reject with { message, code, status } (codes as in the docs, e.g. origin_not_allowed, insufficient_balance).
 * The page's website must be listed under API key -> Websites in the dashboard.
 */
(function () {
  'use strict';
  var src = new URL(document.currentScript.src);
  var KEY = src.searchParams.get('key') || '';
  var BASE = src.origin + '/v1';

  function fail(message, code, status) {
    var e = new Error(message);
    e.code = code;
    e.status = status;
    return e;
  }

  // POST to the API with the key; resolves to the JSON body or rejects with the API's error.
  function call(path, body) {
    return fetch(BASE + path, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (r.ok) return d;
        var err = d.error || {};
        throw fail(err.message || 'Request failed.', err.code || 'server_error', r.status);
      });
    }, function () {
      throw fail('Could not reach the Maps API.', 'network_error', 0);
    });
  }

  // GPS: the first fix within 30 m; otherwise the best one 2 s after the first fix (a phone's GPS sharpens, a
  // laptop's Wi-Fi fix doesn't); at most 10 s.
  function fix() {
    return new Promise(function (resolve, reject) {
      if (!('geolocation' in navigator)) return reject(fail('This browser can’t share location.', 'location_unavailable', 0));
      var best = null;
      var settle = null;
      var stop = function () {
        navigator.geolocation.clearWatch(watch);
        clearTimeout(timer);
        clearTimeout(settle);
      };
      var finish = function () {
        stop();
        best ? resolve(best) : reject(fail('Could not get a location fix.', 'location_unavailable', 0));
      };
      var watch = navigator.geolocation.watchPosition(function (p) {
        if (!best || p.coords.accuracy < best.coords.accuracy) best = p;
        if (p.coords.accuracy <= 30) return finish();
        if (!settle) settle = setTimeout(finish, 2000);
      }, function (e) {
        if (e.code !== e.PERMISSION_DENIED) return;
        stop();
        reject(fail('Location permission was denied.', 'location_denied', 0));
      }, { enableHighAccuracy: true, maximumAge: 0 });
      var timer = setTimeout(finish, 10000);
    });
  }

  // Address from OpenStreetMap (Nominatim), or null.
  function address(lat, lng) {
    return fetch('https://nominatim.openstreetmap.org/reverse?format=json&zoom=18&lat=' + lat + '&lon=' + lng)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return (d && d.display_name) || null; }, function () { return null; });
  }

  window.MapsAPI = {
    locate: function () {
      return fix().then(function (p) {
        var body = { lat: +p.coords.latitude.toFixed(7), lng: +p.coords.longitude.toFixed(7), accuracy_m: +p.coords.accuracy.toFixed(1) };
        return Promise.all([call('/maps/locate', body), address(body.lat, body.lng)]).then(function (r) {
          r[0].address = r[1];
          return r[0];
        });
      });
    },
  };
})();
