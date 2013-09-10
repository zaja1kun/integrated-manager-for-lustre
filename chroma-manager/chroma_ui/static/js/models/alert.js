//
// INTEL CONFIDENTIAL
//
// Copyright 2013 Intel Corporation All Rights Reserved.
//
// The source code contained or described herein and all documents related
// to the source code ("Material") are owned by Intel Corporation or its
// suppliers or licensors. Title to the Material remains with Intel Corporation
// or its suppliers and licensors. The Material contains trade secrets and
// proprietary and confidential information of Intel or its suppliers and
// licensors. The Material is protected by worldwide copyright and trade secret
// laws and treaty provisions. No part of the Material may be used, copied,
// reproduced, modified, published, uploaded, posted, transmitted, distributed,
// or disclosed in any way without Intel's prior express written permission.
//
// No license under any patent, copyright, trade secret or other intellectual
// property right is granted to or conferred upon you by disclosure or delivery
// of the Materials, either expressly, by implication, inducement, estoppel or
// otherwise. Any license under such intellectual property rights must be
// express and approved by Intel in writing.


/**
 * A Factory for accessing alerts.
 */
angular.module('models').factory('alertModel', ['baseModel', 'STATES', function (baseModel, STATES) {
  'use strict';

  return baseModel({
    url: '/api/alert/:alertId',
    params: {alertId: '@id'},
    methods: {
      /**
       * @description Returns the severity of the alert as it's state.
       * @returns {string}
       */
      getState: function () {
        return this.severity;
      },
      getName: function () {
        return 'alert';
      },
      /**
       * Alert should not be dismissed if it's active and has an error or warn level.
       * @returns {boolean}
       */
      notDismissable: function () {
        return (this.severity === STATES.ERROR || this.severity === STATES.WARN) && this.active;
      }
    }
  });
}]);

