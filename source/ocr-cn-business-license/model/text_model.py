# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import copy
import math
import time
import os

import numpy as np
import onnxruntime
from PIL import Image, ImageDraw, ImageFilter, ExifTags

from imaug import create_operators, transform
from postprocess import build_post_process


onnxruntime.set_default_logger_severity(3)

def draw_ocr_box_txt(image,
                     boxes,
                     txts,
                     scores=None,
                     drop_score=0.5):
    h, w = image.height, image.width
    img_left = image.copy()
    img_right = Image.new('RGB', (w, h), (255, 255, 255))

    import random

    random.seed(0)
    draw_left = ImageDraw.Draw(img_left)
    for idx, (box, txt) in enumerate(zip(boxes, txts)):
        if scores is not None and scores[idx] < drop_score:
            continue
        color = (random.randint(0, 255), random.randint(0, 255),
                 random.randint(0, 255))
        draw_left.polygon(box, fill=color)
        box_height = math.sqrt((box[0][0] - box[3][0])**2 + (box[0][1] - box[3][
            1])**2)
        box_width = math.sqrt((box[0][0] - box[1][0])**2 + (box[0][1] - box[1][
            1])**2)
    img_left = Image.blend(image, img_left, 0.5)
    img_show = Image.new('RGB', (w, h), (255, 255, 255))
    img_show.paste(img_left, (0, 0, w, h))
    return img_left
class TextClassifier():
    def __init__(self):
        self.weights_path = os.environ['MODEL_PATH'] + 'classifier.onnx'

        self.cls_image_shape = [3, 48, 192]
        self.cls_batch_num = 30
        self.cls_thresh = 0.9
        self.use_zero_copy_run = False
        postprocess_params = {
            'name': 'ClsPostProcess',
            "label_list": ['0', '180'],
        }
        self.postprocess_op = build_post_process(postprocess_params)

        self.ort_session = onnxruntime.InferenceSession(self.weights_path)

    def resize_norm_img(self, img):
        imgC, imgH, imgW = self.cls_image_shape
        h = img.shape[0]
        w = img.shape[1]
        ratio = w / float(h)
        if math.ceil(imgH * ratio) > imgW:
            resized_w = imgW
        else:
            resized_w = int(math.ceil(imgH * ratio))
        resized_image = np.array(Image.fromarray(img).resize((resized_w, imgH)))
        #resized_image = cv2.resize(img, (resized_w, imgH))
        resized_image = resized_image.astype('float32')
        if self.cls_image_shape[0] == 1:
            resized_image = resized_image / 255
            resized_image = resized_image[np.newaxis, :]
        else:
            resized_image = resized_image.transpose((2, 0, 1)) / 255
        resized_image -= 0.5
        resized_image /= 0.5
        padding_im = np.zeros((imgC, imgH, imgW), dtype=np.float32)
        padding_im[:, :, 0:resized_w] = resized_image
        return padding_im

    def __call__(self, img_list):
        img_list = copy.deepcopy(img_list)
        img_num = len(img_list)
        # Calculate the aspect ratio of all text bars
        width_list = []
        for img in img_list:
            width_list.append(img.shape[1] / float(img.shape[0]))
        # Sorting can speed up the cls process
        indices = np.argsort(np.array(width_list))

        cls_res = [['', 0.0]] * img_num
        batch_num = self.cls_batch_num
        for beg_img_no in range(0, img_num, batch_num):
            end_img_no = min(img_num, beg_img_no + batch_num)
            norm_img_batch = []
            max_wh_ratio = 0
            for ino in range(beg_img_no, end_img_no):
                h, w = img_list[indices[ino]].shape[0:2]
                wh_ratio = w * 1.0 / h
                max_wh_ratio = max(max_wh_ratio, wh_ratio)
            for ino in range(beg_img_no, end_img_no):
                norm_img = self.resize_norm_img(img_list[indices[ino]])
                norm_img = norm_img[np.newaxis, :]
                norm_img_batch.append(norm_img)
            norm_img_batch = np.concatenate(norm_img_batch)
            norm_img_batch = norm_img_batch.copy()
            starttime = time.time()
            ort_inputs = {self.ort_session.get_inputs()[0].name: norm_img_batch}
            prob_out = self.ort_session.run(None, ort_inputs)[0]
            cls_result = self.postprocess_op(prob_out)
            for rno in range(len(cls_result)):
                label, score = cls_result[rno]
                cls_res[indices[beg_img_no + rno]] = [label, score]
                if '180' in label and score > self.cls_thresh:
                    img_list[indices[beg_img_no + rno]] = np.array(Image.fromarray(img_list[indices[beg_img_no + rno]]).transpose(Image.ROTATE_180))
        return img_list, cls_res

class TextDetector():
    def __init__(self):
        modelName = 'det_' + os.environ['MODEL_NAME'] + '.onnx'
        self.weights_path = os.environ['MODEL_PATH'] + modelName

        self.det_algorithm = 'DB'
        self.use_zero_copy_run = False

        pre_process_list = [{
            'DetResizeForTest': {
                'limit_side_len': 960,
                'limit_type': 'max'
            }
        }, {
            'NormalizeImage': {
                'std': [0.229, 0.224, 0.225],
                'mean': [0.485, 0.456, 0.406],
                'scale': '1./255.',
                'order': 'hwc'
            }
        }, {
            'ToCHWImage': None
        }, {
            'KeepKeys': {
                'keep_keys': ['image', 'shape']
            }
        }]

        postprocess_params = {}
        postprocess_params['name'] = 'DBPostProcess'
        postprocess_params["thresh"] = 0.3
        postprocess_params["box_thresh"] = 0.3
        postprocess_params["max_candidates"] = 1000
        postprocess_params["unclip_ratio"] = 1.6
        postprocess_params["use_dilation"] = True
        self.preprocess_op = create_operators(pre_process_list)
        self.postprocess_op = build_post_process(postprocess_params)
        self.ort_session = onnxruntime.InferenceSession(self.weights_path)


    # load_pytorch_weights

    def order_points_clockwise(self, pts):
        """
        reference from: https://github.com/jrosebr1/imutils/blob/master/imutils/perspective.py
        # sort the points based on their x-coordinates
        """
        xSorted = pts[np.argsort(pts[:, 0]), :]

        # grab the left-most and right-most points from the sorted
        # x-roodinate points
        leftMost = xSorted[:2, :]
        rightMost = xSorted[2:, :]

        # now, sort the left-most coordinates according to their
        # y-coordinates so we can grab the top-left and bottom-left
        # points, respectively
        leftMost = leftMost[np.argsort(leftMost[:, 1]), :]
        (tl, bl) = leftMost

        rightMost = rightMost[np.argsort(rightMost[:, 1]), :]
        (tr, br) = rightMost

        rect = np.array([tl, tr, br, bl], dtype="float32")
        return rect

    def clip_det_res(self, points, img_height, img_width):
        for pno in range(points.shape[0]):
            points[pno, 0] = int(min(max(points[pno, 0], 0), img_width - 1))
            points[pno, 1] = int(min(max(points[pno, 1], 0), img_height - 1))
        return points

    def filter_tag_det_res(self, dt_boxes, image_shape):
        img_height, img_width = image_shape[0:2]
        dt_boxes_new = []
        for box in dt_boxes:
            box = self.order_points_clockwise(box)
            box = self.clip_det_res(box, img_height, img_width)
            rect_width = int(np.linalg.norm(box[0] - box[1]))
            rect_height = int(np.linalg.norm(box[0] - box[3]))
            if rect_width <= 3 or rect_height <= 3:
                continue
            dt_boxes_new.append(box)
        dt_boxes = np.array(dt_boxes_new)
        return dt_boxes

    def filter_tag_det_res_only_clip(self, dt_boxes, image_shape):
        img_height, img_width = image_shape[0:2]
        dt_boxes_new = []
        for box in dt_boxes:
            box = self.clip_det_res(box, img_height, img_width)
            dt_boxes_new.append(box)
        dt_boxes = np.array(dt_boxes_new)
        return dt_boxes

    def __call__(self, img):
        ori_im = img.copy()
        data = {'image': img}
        data = transform(data, self.preprocess_op)
        img, shape_list = data
        if img is None:
            return None, 0
        img = np.expand_dims(img, axis=0)
        shape_list = np.expand_dims(shape_list, axis=0)
        img = img.copy()
        ort_inputs = {self.ort_session.get_inputs()[0].name: img}
        preds = {}
        preds['maps'] = self.ort_session.run(None, ort_inputs)[0]

        post_result = self.postprocess_op(preds, shape_list)
        dt_boxes = post_result[0]['points']
        if self.det_algorithm == "SAST" and self.det_sast_polygon:
            dt_boxes = self.filter_tag_det_res_only_clip(dt_boxes, ori_im.shape)
        else:
            dt_boxes = self.filter_tag_det_res(dt_boxes, ori_im.shape)
        return dt_boxes

class TextRecognizer():
    def __init__(self):
        modelName = 'rec_' + os.environ['MODEL_NAME'] + '.onnx'
        self.weights_path = os.environ['MODEL_PATH'] + modelName

        self.limited_max_width = 1280
        self.limited_min_width = 16

        self.rec_image_shape = [3, 32, 320]
        self.character_type = 'ch'
        self.rec_batch_num = 6
        self.rec_algorithm = 'CRNN'
        self.use_zero_copy_run = False
        postprocess_params = {
            'name': 'CTCLabelDecode',
            "character_type": 'ch',
            "character_dict_path": os.environ['MODEL_PATH'] + 'keys_v1.txt',
            "use_space_char": True
        }
        self.postprocess_op = build_post_process(postprocess_params)

        self.ort_session = onnxruntime.InferenceSession(self.weights_path)


    def resize_norm_img(self, img, max_wh_ratio):
        imgC, imgH, imgW = self.rec_image_shape
        assert imgC == img.shape[2]
        if self.character_type == "ch":
            imgW = int((32 * max_wh_ratio))
        imgW = max(min(imgW, self.limited_max_width), self.limited_min_width)
        h, w = img.shape[:2]
        ratio = w / float(h)
        ratio_imgH = math.ceil(imgH * ratio)
        ratio_imgH = max(ratio_imgH, self.limited_min_width)
        if ratio_imgH > imgW:
            resized_w = imgW
        else:
            resized_w = int(math.ceil(imgH * ratio))
        resized_image = np.array(Image.fromarray(img).resize((resized_w, imgH)))
        #resized_image = cv2.resize(img, (resized_w, imgH))
        resized_image = resized_image.astype('float32')
        resized_image = resized_image.transpose((2, 0, 1)) / 255
        resized_image -= 0.5
        resized_image /= 0.5
        padding_im = np.zeros((imgC, imgH, imgW), dtype=np.float32)
        padding_im[:, :, 0:resized_w] = resized_image
        return padding_im

    def __call__(self, img_list):
        img_num = len(img_list)
        # Calculate the aspect ratio of all text bars
        width_list = []
        for img in img_list:
            width_list.append(img.shape[1] / float(img.shape[0]))
        # Sorting can speed up the recognition process
        indices = np.argsort(np.array(width_list))

        # rec_res = []
        rec_res = [['', 0.0]] * img_num
        batch_num = self.rec_batch_num
        for beg_img_no in range(0, img_num, batch_num):
            end_img_no = min(img_num, beg_img_no + batch_num)
            norm_img_batch = []
            max_wh_ratio = 0
            for ino in range(beg_img_no, end_img_no):
                # h, w = img_list[ino].shape[0:2]
                h, w = img_list[indices[ino]].shape[0:2]
                wh_ratio = w * 1.0 / h
                max_wh_ratio = max(max_wh_ratio, wh_ratio)
            for ino in range(beg_img_no, end_img_no):
                # norm_img = self.resize_norm_img(img_list[ino], max_wh_ratio)
                norm_img = self.resize_norm_img(img_list[indices[ino]],
                                                max_wh_ratio)
                norm_img = norm_img[np.newaxis, :]
                norm_img_batch.append(norm_img)
            norm_img_batch = np.concatenate(norm_img_batch)
            norm_img_batch = norm_img_batch.copy()
            ort_inputs = {self.ort_session.get_inputs()[0].name: norm_img_batch}
            preds = self.ort_session.run(None, ort_inputs)[0]

            rec_result = self.postprocess_op(preds)
            for rno in range(len(rec_result)):
                rec_res[indices[beg_img_no + rno]] = rec_result[rno]
        return rec_res