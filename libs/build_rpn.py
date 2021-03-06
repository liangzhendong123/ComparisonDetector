# # -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow.contrib.slim as slim

from libs.box_utils import make_anchor
from libs.box_utils import boxes_utils
from libs.box_utils import encode_and_decode

from libs import losses


class RPN(object):
    def __init__(self,
                 feature_pyramid,
                 image_window,
                 config):

        self.feature_pyramid = feature_pyramid
        self.window = image_window
        self.config = config
        self.anchor_ratios = tf.constant(config.ANCHOR_RATIOS, dtype=tf.float32)
        self.num_of_anchors_per_location = len(config.ANCHOR_RATIOS)
        self.rpn_encode_boxes, self.rpn_scores = self.rpn_net()

    def rpn_net(self):
        """
        base the anchor stride to compute the output(batch_size, object_scores, pred_bbox) of every feature map,
        now it supported the multi image in a gpu
        return
        rpn_all_encode_boxes(batch_size, all_anchors, 4)
        rpn_all_boxes_scores(batch_size, all_anchors, 2)
        Be Cautious:
        all_anchors is concat by the order of ［P2, P3, P4, P5, P6］ which request that
        the anchors must be generated by this order.
        """

        rpn_encode_boxes_list = []
        rpn_scores_list = []

        with tf.variable_scope('rpn_net'):
            with slim.arg_scope([slim.conv2d],
                                weights_initializer=tf.glorot_uniform_initializer(),
                                weights_regularizer=slim.l2_regularizer(self.config.WEIGHT_DECAY)):
                for level in self.config.LEVEL:
                    reuse_flag = tf.AUTO_REUSE
                    scope_list = ['conv2d_3x3', 'rpn_classifier', 'rpn_regressor']

                    rpn_conv2d_3x3 = slim.conv2d(inputs=self.feature_pyramid[level],
                                                 num_outputs=512,
                                                 kernel_size=[3, 3],
                                                 stride=self.config.RPN_ANCHOR_STRIDE,
                                                 scope=scope_list[0],
                                                 reuse=reuse_flag)
                    rpn_box_scores = slim.conv2d(rpn_conv2d_3x3,
                                                 num_outputs=2 * self.num_of_anchors_per_location,
                                                 kernel_size=[1, 1],
                                                 stride=1,
                                                 scope=scope_list[1],
                                                 activation_fn=None,
                                                 reuse=reuse_flag)
                    rpn_encode_boxes = slim.conv2d(rpn_conv2d_3x3,
                                                   num_outputs=4 * self.num_of_anchors_per_location,
                                                   kernel_size=[1, 1],
                                                   stride=1,
                                                   scope=scope_list[2],
                                                   activation_fn=None,
                                                   reuse=reuse_flag)
                    rpn_box_scores = tf.reshape(rpn_box_scores, [self.config.PER_GPU_IMAGE, -1, 2])
                    rpn_encode_boxes = tf.reshape(rpn_encode_boxes, [self.config.PER_GPU_IMAGE, -1, 4])

                    rpn_scores_list.append(rpn_box_scores)
                    rpn_encode_boxes_list.append(rpn_encode_boxes)

                rpn_all_encode_boxes = tf.concat(rpn_encode_boxes_list, axis=1)
                rpn_all_boxes_scores = tf.concat(rpn_scores_list, axis=1)

            return rpn_all_encode_boxes, rpn_all_boxes_scores

    def rpn_losses(self, minibatch_indices, minibatch_encode_gtboxes, minibatch_labels_one_hot):
        """
        :param minibatch_indices: (batch_size, config.RPN_MINIBATCH_SIZE)
        :param minibatch_encode_gtboxes: (batch_size, config.RPN_MINIBATCH_SIZE, 4)
        :param minibatch_labels_one_hot: (batch_size, config.RPN_MINIBATCH_SIZE, 2)
        :return: the mean of location_loss, classification_loss
        """
        with tf.variable_scope("rpn_losses"):

            def batch_slice_rpn_target(mini_indices, rpn_encode_boxes, rpn_scores):
                """
                :param mini_indices: (config.RPN_MINIBATCH_SIZE, ) this is indices of anchors
                :param rpn_encode_boxes: (config.RPN_MINIBATCH_SIZE, 4)
                :param rpn_scores: (config.RPN_MINIBATCH_SIZE, 2)
                """
                mini_encode_boxes = tf.gather(rpn_encode_boxes, mini_indices)
                mini_boxes_scores = tf.gather(rpn_scores, mini_indices)

                return mini_encode_boxes, mini_boxes_scores

            mini_encode_boxes, mini_boxes_scores = boxes_utils.batch_slice([minibatch_indices,
                                                                            self.rpn_encode_boxes,
                                                                            self.rpn_scores],
                                                                            lambda x, y, z: batch_slice_rpn_target(x, y, z),
                                                                            self.config.PER_GPU_IMAGE)

            object_mask = tf.cast(minibatch_labels_one_hot[:, :, 1], tf.float32)
            # losses
            with tf.variable_scope('rpn_location_loss'):
                location_loss = losses.l1_smooth_losses(predict_boxes=mini_encode_boxes,
                                                        gtboxes=minibatch_encode_gtboxes,
                                                        object_weights=object_mask)

            with tf.variable_scope('rpn_classification_loss'):

                classification_loss = tf.losses.softmax_cross_entropy(logits=mini_boxes_scores,
                                                                      onehot_labels=minibatch_labels_one_hot)
                classification_loss = tf.cond(tf.is_nan(classification_loss), lambda: 0.0, lambda: classification_loss)

            return location_loss, classification_loss

    def rpn_proposals(self, is_training):
        """
        :param is_training:
        :return:
        rpn_proposals_boxes: (batch_size, config.MAX_PROPOSAL_SIZE, 4)(y1, x1, y2, x2)
        """
        anchors = make_anchor.generate_pyramid_anchors(self.config)
        if is_training:
            rpn_proposals_num = self.config.MAX_PROPOSAL_NUM_TRAINING
        else:
            rpn_proposals_num = self.config.MAX_PROPOSAL_NUM_INFERENCE

        def batch_slice_rpn_proposals(rpn_encode_boxes, rpn_scores, anchors, config, rpn_proposals_num):

            with tf.variable_scope('rpn_proposals'):
                rpn_softmax_scores = slim.softmax(rpn_scores)
                rpn_object_score = rpn_softmax_scores[:, 1]  # second column represent object
                if config.RPN_TOP_K_NMS:
                    top_k_indices = tf.nn.top_k(rpn_object_score, k=config.RPN_TOP_K_NMS).indices
                    rpn_object_score = tf.gather(rpn_object_score, top_k_indices)
                    rpn_encode_boxes = tf.gather(rpn_encode_boxes, top_k_indices)
                    anchors = tf.gather(anchors, top_k_indices)
                
                rpn_decode_boxes = encode_and_decode.decode_boxes(encode_boxes=rpn_encode_boxes,
                                                                  reference_boxes=anchors,
                                                                  dev_factors=config.RPN_BBOX_STD_DEV)
                   
                valid_indices = boxes_utils.non_maximal_suppression(boxes=rpn_decode_boxes,
                                                                    scores=rpn_object_score,
                                                                    max_output_size=rpn_proposals_num,
                                                                    iou_threshold=config.RPN_NMS_IOU_THRESHOLD)
                rpn_decode_boxes = tf.gather(rpn_decode_boxes, valid_indices)
                rpn_object_score = tf.gather(rpn_object_score, valid_indices)
                # clip proposals to img boundaries(replace the out boundary with image boundary)
                rpn_decode_boxes = boxes_utils.clip_boxes_to_img_boundaries(rpn_decode_boxes, [0, 0,
                                                                                               config.TARGET_SIDE-1,
                                                                                               config.TARGET_SIDE-1])
                # Pad if needed
                padding = tf.maximum(rpn_proposals_num - tf.shape(rpn_decode_boxes)[0], 0)
                # care about why we don't use tf.pad in there
                zeros_padding = tf.zeros((padding, 4), dtype=tf.float32)
                rpn_proposals_boxes = tf.concat([rpn_decode_boxes, zeros_padding], axis=0)
                rpn_object_score = tf.pad(rpn_object_score, [(0, padding)])

                return rpn_proposals_boxes, rpn_object_score

        rpn_proposals_boxes, rpn_object_scores = \
            boxes_utils.batch_slice([self.rpn_encode_boxes, self.rpn_scores],
                                    lambda x, y: batch_slice_rpn_proposals(x, y, anchors,
                                                                              self.config, rpn_proposals_num),
                                    self.config.PER_GPU_IMAGE)

        return rpn_proposals_boxes, rpn_object_scores