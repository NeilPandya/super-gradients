import math
import os
import pathlib
import warnings
from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable, List, Union, Tuple, Optional

import cv2

import numpy as np
import torch
import torchvision
from omegaconf import ListConfig
from torch import nn

from super_gradients.training.utils.visualization.detection import draw_bbox
from super_gradients.training.utils.visualization.utils import generate_color_mapping
from super_gradients.common.deprecate import deprecate_param


class DetectionTargetsFormat(Enum):
    """
    Enum class for the different detection output formats

    When NORMALIZED is not specified- the type refers to unnormalized image coordinates (of the bboxes).

    For example:
    LABEL_NORMALIZED_XYXY means [class_idx,x1,y1,x2,y2]
    """

    LABEL_XYXY = "LABEL_XYXY"
    XYXY_LABEL = "XYXY_LABEL"
    LABEL_NORMALIZED_XYXY = "LABEL_NORMALIZED_XYXY"
    NORMALIZED_XYXY_LABEL = "NORMALIZED_XYXY_LABEL"
    LABEL_CXCYWH = "LABEL_CXCYWH"
    CXCYWH_LABEL = "CXCYWH_LABEL"
    LABEL_NORMALIZED_CXCYWH = "LABEL_NORMALIZED_CXCYWH"
    NORMALIZED_CXCYWH_LABEL = "NORMALIZED_CXCYWH_LABEL"


def get_class_index_in_target(target_format: DetectionTargetsFormat) -> int:
    """Get the label of a given target
    :param target_format:   Representation of the target (ex: LABEL_XYXY)
    :return:                Position of the class id in a bbox
                                ex: 0 if bbox of format label_xyxy | -1 if bbox of format xyxy_label
    """
    format_split = target_format.value.split("_")
    if format_split[0] == "LABEL":
        return 0
    elif format_split[-1] == "LABEL":
        return -1
    else:
        raise NotImplementedError(f"No implementation to find index of LABEL in {target_format.value}")


def _set_batch_labels_index(labels_batch):
    for i, labels in enumerate(labels_batch):
        labels[:, 0] = i
    return labels_batch


def convert_cxcywh_bbox_to_xyxy(input_bbox: torch.Tensor):
    """
    Converts bounding box format from [cx, cy, w, h] to [x1, y1, x2, y2]
        :param input_bbox:  input bbox either 2-dimensional (for all boxes of a single image) or 3-dimensional (for
                            boxes of a batch of images)
        :return:            Converted bbox in same dimensions as the original
    """
    need_squeeze = False
    # the input is always processed as a batch. in case it not a batch, it is unsqueezed, process and than squeeze back.
    if input_bbox.dim() < 3:
        need_squeeze = True
        input_bbox = input_bbox.unsqueeze(0)

    converted_bbox = torch.zeros_like(input_bbox) if isinstance(input_bbox, torch.Tensor) else np.zeros_like(input_bbox)
    converted_bbox[:, :, 0] = input_bbox[:, :, 0] - input_bbox[:, :, 2] / 2
    converted_bbox[:, :, 1] = input_bbox[:, :, 1] - input_bbox[:, :, 3] / 2
    converted_bbox[:, :, 2] = input_bbox[:, :, 0] + input_bbox[:, :, 2] / 2
    converted_bbox[:, :, 3] = input_bbox[:, :, 1] + input_bbox[:, :, 3] / 2

    # squeeze back if needed
    if need_squeeze:
        converted_bbox = converted_bbox[0]

    return converted_bbox


def _iou(CIoU: bool, DIoU: bool, GIoU: bool, b1_x1, b1_x2, b1_y1, b1_y2, b2_x1, b2_x2, b2_y1, b2_y2, eps):
    """
    Internal function for the use of calculate_bbox_iou_matrix and calculate_bbox_iou_elementwise functions
    DO NOT CALL THIS FUNCTIONS DIRECTLY - use one of the functions mentioned above
    """
    # Intersection area
    intersection_area = (torch.min(b1_x2, b2_x2) - torch.max(b1_x1, b2_x1)).clamp(0) * (torch.min(b1_y2, b2_y2) - torch.max(b1_y1, b2_y1)).clamp(0)
    # Union Area
    w1, h1 = b1_x2 - b1_x1, b1_y2 - b1_y1
    w2, h2 = b2_x2 - b2_x1, b2_y2 - b2_y1
    union_area = w1 * h1 + w2 * h2 - intersection_area + eps
    iou = intersection_area / union_area  # iou
    if GIoU or DIoU or CIoU:
        cw = torch.max(b1_x2, b2_x2) - torch.min(b1_x1, b2_x1)  # convex (smallest enclosing box) width
        ch = torch.max(b1_y2, b2_y2) - torch.min(b1_y1, b2_y1)  # convex height
        # Generalized IoU https://arxiv.org/pdf/1902.09630.pdf
        if GIoU:
            c_area = cw * ch + eps  # convex area
            iou -= (c_area - union_area) / c_area  # GIoU
        # Distance or Complete IoU https://arxiv.org/abs/1911.08287v1
        if DIoU or CIoU:
            # convex diagonal squared
            c2 = cw**2 + ch**2 + eps
            # centerpoint distance squared
            rho2 = ((b2_x1 + b2_x2 - b1_x1 - b1_x2) ** 2 + (b2_y1 + b2_y2 - b1_y1 - b1_y2) ** 2) / 4
            if DIoU:
                iou -= rho2 / c2  # DIoU
            elif CIoU:  # https://github.com/Zzh-tju/DIoU-SSD-pytorch/blob/master/utils/box/box_utils.py#L47
                v = (4 / math.pi**2) * torch.pow(torch.atan(w2 / h2) - torch.atan(w1 / h1), 2)
                with torch.no_grad():
                    alpha = v / ((1 + eps) - iou + v)
                iou -= rho2 / c2 + v * alpha  # CIoU
    return iou


def calculate_bbox_iou_matrix(box1, box2, x1y1x2y2=True, GIoU: bool = False, DIoU=False, CIoU=False, eps=1e-9):
    """
    calculate iou matrix containing the iou of every couple iuo(i,j) where i is in box1 and j is in box2
    :param box1: a 2D tensor of boxes (shape N x 4)
    :param box2: a 2D tensor of boxes (shape M x 4)
    :param x1y1x2y2: boxes format is x1y1x2y2 (True) or xywh where xy is the center (False)
    :return: a 2D iou matrix (shape NxM)
    """
    if box1.dim() > 1:
        box1 = box1.T

    # Get the coordinates of bounding boxes
    if x1y1x2y2:  # x1, y1, x2, y2 = box1
        b1_x1, b1_y1, b1_x2, b1_y2 = box1[0], box1[1], box1[2], box1[3]
        b2_x1, b2_y1, b2_x2, b2_y2 = box2[:, 0], box2[:, 1], box2[:, 2], box2[:, 3]
    else:  # x, y, w, h = box1
        b1_x1, b1_x2 = box1[0] - box1[2] / 2, box1[0] + box1[2] / 2
        b1_y1, b1_y2 = box1[1] - box1[3] / 2, box1[1] + box1[3] / 2
        b2_x1, b2_x2 = box2[:, 0] - box2[:, 2] / 2, box2[:, 0] + box2[:, 2] / 2
        b2_y1, b2_y2 = box2[:, 1] - box2[:, 3] / 2, box2[:, 1] + box2[:, 3] / 2

    b1_x1, b1_y1, b1_x2, b1_y2 = b1_x1.unsqueeze(1), b1_y1.unsqueeze(1), b1_x2.unsqueeze(1), b1_y2.unsqueeze(1)

    return _iou(CIoU, DIoU, GIoU, b1_x1, b1_x2, b1_y1, b1_y2, b2_x1, b2_x2, b2_y1, b2_y2, eps)


def calc_bbox_iou_matrix(pred: torch.Tensor):
    """
    calculate iou for every pair of boxes in the boxes vector
    :param pred: a 3-dimensional tensor containing all boxes for a batch of images [N, num_boxes, 4], where
                 each box format is [x1,y1,x2,y2]
    :return: a 3-dimensional matrix where M_i_j_k is the iou of box j and box k of the i'th image in the batch
    """
    box = pred[:, :, :4]  #
    b1_x1, b1_y1 = box[:, :, 0].unsqueeze(1), box[:, :, 1].unsqueeze(1)
    b1_x2, b1_y2 = box[:, :, 2].unsqueeze(1), box[:, :, 3].unsqueeze(1)

    b2_x1 = b1_x1.transpose(2, 1)
    b2_x2 = b1_x2.transpose(2, 1)
    b2_y1 = b1_y1.transpose(2, 1)
    b2_y2 = b1_y2.transpose(2, 1)
    intersection_area = (torch.min(b1_x2, b2_x2) - torch.max(b1_x1, b2_x1)).clamp(0) * (torch.min(b1_y2, b2_y2) - torch.max(b1_y1, b2_y1)).clamp(0)
    # Union Area
    w1, h1 = b1_x2 - b1_x1, b1_y2 - b1_y1
    w2, h2 = b2_x2 - b2_x1, b2_y2 - b2_y1
    union_area = (w1 * h1 + 1e-16) + w2 * h2 - intersection_area
    ious = intersection_area / union_area
    return ious


def change_bbox_bounds_for_image_size_inplace(boxes: np.ndarray, img_shape: Tuple[int, int]) -> np.ndarray:
    """
    Clips bboxes to image boundaries. The function operates in-place.

    :param bboxes:     (np.ndarray) Input bounding boxes in XYXY format of [..., 4] shape
    :param img_shape:  Tuple[int,int] of image shape (height, width).
    :return:           (np.ndarray)clipped bboxes in XYXY format of [..., 4] shape
    """
    boxes[..., [0, 2]] = boxes[..., [0, 2]].clip(min=0, max=img_shape[1])
    boxes[..., [1, 3]] = boxes[..., [1, 3]].clip(min=0, max=img_shape[0])
    return boxes


def change_bbox_bounds_for_image_size(boxes: np.ndarray, img_shape: Tuple[int, int], inplace=True) -> np.ndarray:
    """
    Clips bboxes to image boundaries.
    The function may operate both in- and on a copy of the input which is controlled by the inplace parameter.
    It exists for backward compatibility and will be removed in the SG 3.8.0 and this method will not modify the input.
    An inplace version of this method is available as change_bbox_bounds_for_image_size_inplace.

    :param bboxes:     (np.ndarray) Input bounding boxes in XYXY format of [..., 4] shape
    :param img_shape:  Tuple[int,int] of image shape (height, width).
    :param inplace:    (bool) If True, the function operates in-place. Otherwise, it returns a modified copy.
                       If True this will trigger a deprecated warning to inform the user to use
                       change_bbox_bounds_for_image_size_inplace instead.
    :return:           (np.ndarray)clipped bboxes in XYXY format of [..., 4] shape
    """
    if not inplace:
        boxes = boxes.copy()
    else:
        deprecate_param(
            deprecated_param_name="inplace",
            deprecated_since="3.7.0",
            removed_from="3.8.0",
            reason="For in-place operation, use change_bbox_bounds_for_image_size_inplace",
        )
    return change_bbox_bounds_for_image_size_inplace(boxes, img_shape)


class DetectionPostPredictionCallback(ABC, nn.Module):
    def __init__(self) -> None:
        super().__init__()

    @abstractmethod
    def forward(self, x, device: str = None):
        """

        :param x:       the output of your model
        :param device:  (Deprecated) Not used anymore, exists only for sake of keeping the same interface as in the parent class.
                        Will be removed in the SG 3.7.0.
                        A device parameter in case we want to move tensors to a specific device.
        :return:        a list with length batch_size, each item in the list is a detections
                        with shape: nx6 (x1, y1, x2, y2, confidence, class) where x and y are in range [0,1]
        """
        raise NotImplementedError


class IouThreshold(tuple, Enum):
    MAP_05 = (0.5, 0.5)
    MAP_05_TO_095 = (0.5, 0.95)

    def is_range(self):
        return self[0] != self[1]

    def to_tensor(self):
        if self.is_range():
            return self.from_bounds(self[0], self[1], step=0.05)
        else:
            return torch.tensor([self[0]])

    @classmethod
    def from_bounds(cls, low: float, high: float, step: float = 0.05) -> torch.Tensor:
        """
        Create a tensor with values from low (including) to high (including) with a given step size.
        :param low: Lower bound
        :param high: Upper bound
        :param step: Step size
        :return: Tensor of [low, low + step, low + 2 * step, ..., high]
        """
        n_iou_thresh = int(round((high - low) / step)) + 1
        return torch.linspace(low, high, n_iou_thresh)


def box_iou(box1: torch.Tensor, box2: torch.Tensor) -> torch.Tensor:
    # https://github.com/pytorch/vision/blob/master/torchvision/ops/boxes.py
    """
    Return intersection-over-union (Jaccard index) of boxes.
    Both sets of boxes are expected to be in (x1, y1, x2, y2) format.
    :param box1: Tensor of shape [N, 4]
    :param box2: Tensor of shape [M, 4]
    :return:     iou, Tensor of shape [N, M]: the NxM matrix containing the pairwise IoU values for every element in boxes1 and boxes2
    """

    def box_area(box):
        # box = 4xn
        return (box[2] - box[0]) * (box[3] - box[1])

    area1 = box_area(box1.T)
    area2 = box_area(box2.T)

    # inter(N,M) = (rb(N,M,2) - lt(N,M,2)).clamp(0).prod(2)
    inter = (torch.min(box1[:, None, 2:], box2[:, 2:]) - torch.max(box1[:, None, :2], box2[:, :2])).clamp(0).prod(2)
    return inter / (area1[:, None] + area2 - inter)  # iou = inter / (area1 + area2 - inter)


def non_max_suppression(
    prediction, conf_thres=0.1, iou_thres=0.6, multi_label_per_box: bool = True, with_confidence: bool = False, class_agnostic_nms: bool = False
):
    """
    Performs Non-Maximum Suppression (NMS) on inference results

    :param prediction: raw model prediction. Should be a list of Tensors of shape (cx, cy, w, h, confidence, cls0, cls1, ...)
    :param conf_thres: below the confidence threshold - prediction are discarded
    :param iou_thres: IoU threshold for the nms algorithm
    :param multi_label_per_box: controls whether to decode multiple labels per box.
                                True - each anchor can produce multiple labels of different classes
                                       that pass confidence threshold check (default).
                                False - each anchor can produce only one label of the class with the highest score.
    :param with_confidence: whether to multiply objectness score with class score.
                            usually valid for Yolo models only.
    :param class_agnostic_nms: indicates how boxes of different classes will be treated during NMS
                               True - NMS will be performed on all classes together.
                               False - NMS will be performed on each class separately (default).
    :return: detections with shape nx6 (x1, y1, x2, y2, object_conf, class_conf, class)

    """
    candidates_above_thres = prediction[..., 4] > conf_thres  # filter by confidence
    output = [None] * prediction.shape[0]

    for image_idx, pred in enumerate(prediction):
        pred = pred[candidates_above_thres[image_idx]]  # confident

        if not pred.shape[0]:  # If none remain process next image
            continue

        if with_confidence:
            pred[:, 5:] *= pred[:, 4:5]  # multiply objectness score with class score

        box = convert_cxcywh_bbox_to_xyxy(pred[:, :4])  # cxcywh to xyxy

        # Detections matrix nx6 (xyxy, conf, cls)
        if multi_label_per_box:  # try for all good confidence classes
            i, j = (pred[:, 5:] > conf_thres).nonzero(as_tuple=False).T
            pred = torch.cat((box[i], pred[i, j + 5, None], j[:, None].float()), 1)

        else:  # best class only
            conf, j = pred[:, 5:].max(1, keepdim=True)
            pred = torch.cat((box, conf, j.float()), 1)[conf.view(-1) > conf_thres]

        if not pred.shape[0]:  # If none remain process next image
            continue

        # Apply torch batched NMS algorithm
        boxes, scores, cls_idx = pred[:, :4], pred[:, 4], pred[:, 5]
        if class_agnostic_nms:
            idx_to_keep = torchvision.ops.boxes.nms(boxes, scores, iou_thres)
        else:
            idx_to_keep = torchvision.ops.boxes.batched_nms(boxes, scores, cls_idx, iou_thres)
        output[image_idx] = pred[idx_to_keep]

    return output


def matrix_non_max_suppression(
    pred, conf_thres: float = 0.1, kernel: str = "gaussian", sigma: float = 3.0, max_num_of_detections: int = 500, class_agnostic_nms: bool = False
) -> List[torch.Tensor]:
    """Performs Matrix Non-Maximum Suppression (NMS) on inference results https://arxiv.org/pdf/1912.04488.pdf

    :param pred:        Raw model prediction (in test mode) - a Tensor of shape [batch, num_predictions, 85]
                        where each item format is (x, y, w, h, object_conf, class_conf, ... 80 classes score ...)
    :param conf_thres:  Threshold under which prediction are discarded
    :param kernel:      Type of kernel to use ['gaussian', 'linear']
    :param sigma:       Sigma for the gaussian kernel
    :param max_num_of_detections: Maximum number of boxes to output

    :return: Detections list with shape (x1, y1, x2, y2, object_conf, class_conf, class)
    """
    # MULTIPLY CONF BY CLASS CONF TO GET COMBINED CONFIDENCE
    class_conf, class_pred = pred[:, :, 5:].max(2)
    pred[:, :, 4] *= class_conf

    # BOX (CENTER X, CENTER Y, WIDTH, HEIGHT) TO (X1, Y1, X2, Y2)
    pred[:, :, :4] = convert_cxcywh_bbox_to_xyxy(pred[:, :, :4])

    # DETECTIONS ORDERED AS (x1y1x2y2, obj_conf, class_conf, class_pred)
    pred = torch.cat((pred[:, :, :5], class_pred.unsqueeze(2)), 2)

    # SORT DETECTIONS BY DECREASING CONFIDENCE SCORES
    sort_ind = (-pred[:, :, 4]).argsort()
    pred = torch.stack([pred[i, sort_ind[i]] for i in range(pred.shape[0])])[:, 0:max_num_of_detections]

    ious = calc_bbox_iou_matrix(pred)

    ious = ious.triu(1)

    if not class_agnostic_nms:
        # CREATE A LABELS MASK, WE WANT ONLY BOXES WITH THE SAME LABEL TO AFFECT EACH OTHER
        labels = pred[:, :, 5:]
        labeles_matrix = (labels == labels.transpose(2, 1)).float().triu(1)
        ious *= labeles_matrix

    ious_cmax, _ = ious.max(1)
    ious_cmax = ious_cmax.unsqueeze(2).repeat(1, 1, max_num_of_detections)

    if kernel == "gaussian":
        decay_matrix = torch.exp(-1 * sigma * (ious**2))
        compensate_matrix = torch.exp(-1 * sigma * (ious_cmax**2))
        decay, _ = (decay_matrix / compensate_matrix).min(dim=1)
    else:
        decay = (1 - ious) / (1 - ious_cmax)
        decay, _ = decay.min(dim=1)

    pred[:, :, 4] *= decay

    output = [pred[i, pred[i, :, 4] > conf_thres] for i in range(pred.shape[0])]

    return output


class NMS_Type(str, Enum):
    """
    Type of non max suppression algorithm that can be used for post processing detection
    """

    ITERATIVE = "iterative"
    MATRIX = "matrix"


def undo_image_preprocessing(im_tensor: torch.Tensor) -> np.ndarray:
    """
    :param im_tensor: images in a batch after preprocessing for inference, RGB, (B, C, H, W)
    :return:          images in a batch in cv2 format, BGR, (B, H, W, C)
    """
    im_np = im_tensor.cpu().numpy()
    im_np = im_np[:, ::-1, :, :].transpose(0, 2, 3, 1)
    im_np *= 255.0
    return np.ascontiguousarray(im_np, dtype=np.uint8)


class DetectionVisualization:
    @staticmethod
    def _generate_color_mapping(num_classes: int) -> List[Tuple[int]]:
        """
        Generate a unique BGR color for each class
        """

        return generate_color_mapping(num_classes=num_classes)

    @staticmethod
    def draw_box_title(
        color_mapping: List[Tuple[int]],
        class_names: List[str],
        box_thickness: Optional[int],
        image_np: np.ndarray,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        class_id: int,
        pred_conf: float = None,
        bbox_prefix: str = "",
    ):
        """
        Draw a rectangle with class name, confidence on the image
        :param color_mapping: A list of N RGB colors for each class
        :param class_names: A list of N class names
        :param box_thickness: Thickness of the bounding box (in pixels)
        :param image_np: Image in RGB format (H, W, C) where to draw the bounding box
        :param x1: X coordinate of the top left corner of the bounding box
        :param y1: Y coordinate of the top left corner of the bounding box
        :param x2: X coordinate of the bottom right corner of the bounding box
        :param y2: Y coordinate of the bottom right corner of the bounding box
        :param class_id: A corresponding class id
        :param pred_conf: Class confidence score (optional)
        :param bbox_prefix: Prefix to add to the title of the bounding boxes
        """
        color = color_mapping[class_id]
        class_name = class_names[class_id]

        title = class_name
        if bbox_prefix:
            title = f"{bbox_prefix} {class_name}"
        if pred_conf is not None:
            title = f"{title} {str(round(pred_conf, 2))}"

        image_np = draw_bbox(image=image_np, title=title, x1=x1, y1=y1, x2=x2, y2=y2, box_thickness=box_thickness, color=color)
        return image_np

    @staticmethod
    def _visualize_image(
        image_np: np.ndarray,
        pred_boxes: np.ndarray,
        target_boxes: np.ndarray,
        class_names: List[str],
        box_thickness: Optional[int],
        gt_alpha: float,
        image_scale: float,
        checkpoint_dir: str,
        image_name: str,
    ):
        return DetectionVisualization.visualize_image(
            image_np=image_np,
            pred_boxes=pred_boxes,
            target_boxes=target_boxes,
            class_names=class_names,
            box_thickness=box_thickness,
            gt_alpha=gt_alpha,
            image_scale=image_scale,
            checkpoint_dir=checkpoint_dir,
            image_name=image_name,
        )

    @staticmethod
    def visualize_image(
        image_np: np.ndarray,
        class_names: List[str],
        target_boxes: Optional[np.ndarray] = None,
        pred_boxes: Optional[np.ndarray] = None,
        box_thickness: Optional[int] = 2,
        gt_alpha: float = 0.6,
        image_scale: float = 1.0,
        checkpoint_dir: Optional[str] = None,
        image_name: Optional[str] = None,
    ):
        image_np = cv2.resize(image_np, (0, 0), fx=image_scale, fy=image_scale, interpolation=cv2.INTER_NEAREST)
        color_mapping = DetectionVisualization._generate_color_mapping(len(class_names))

        if pred_boxes is not None:
            # Draw predictions
            pred_boxes[:, :4] *= image_scale
            for xyxy_score_label in pred_boxes:
                image_np = DetectionVisualization.draw_box_title(
                    color_mapping=color_mapping,
                    class_names=class_names,
                    box_thickness=box_thickness,
                    image_np=image_np,
                    x1=int(xyxy_score_label[0]),
                    y1=int(xyxy_score_label[1]),
                    x2=int(xyxy_score_label[2]),
                    y2=int(xyxy_score_label[3]),
                    class_id=int(xyxy_score_label[5]),
                    pred_conf=float(xyxy_score_label[4]),
                    bbox_prefix="[Pred]" if target_boxes is not None else "",  # If we have TARGETS, we want to add a prefix to distinguish.
                )

        if target_boxes is not None:
            # If gt_alpha is set, we will show it as a transparent overlay.
            if gt_alpha is not None:
                # Transparent overlay of ground truth boxes
                image_with_targets = np.zeros_like(image_np, np.uint8)
            else:
                image_with_targets = image_np

            for label_xyxy in target_boxes:
                image_with_targets = DetectionVisualization.draw_box_title(
                    color_mapping=color_mapping,
                    class_names=class_names,
                    box_thickness=box_thickness,
                    image_np=image_with_targets,
                    x1=int(label_xyxy[1]),
                    y1=int(label_xyxy[2]),
                    x2=int(label_xyxy[3]),
                    y2=int(label_xyxy[4]),
                    class_id=int(label_xyxy[0]),
                    bbox_prefix="[GT]" if pred_boxes is not None else "",  # If we have PREDICTIONS, we want to add a prefix to distinguish.
                )

            if gt_alpha is not None:
                # Transparent overlay of ground truth boxes
                mask = image_with_targets.astype(bool)
                image_np[mask] = cv2.addWeighted(image_np, 1 - gt_alpha, image_with_targets, gt_alpha, 0)[mask]
            else:
                image_np = image_with_targets

        if checkpoint_dir is None:
            return image_np
        else:
            pathlib.Path(checkpoint_dir).mkdir(parents=True, exist_ok=True)
            cv2.imwrite(os.path.join(checkpoint_dir, str(image_name) + ".jpg"), image_np)

    @staticmethod
    def _scaled_ccwh_to_xyxy(target_boxes: np.ndarray, h: int, w: int, image_scale: float) -> np.ndarray:
        """
        Modifies target_boxes inplace
        :param target_boxes:    (c1, c2, w, h) boxes in [0, 1] range
        :param h:               image height
        :param w:               image width
        :param image_scale:     desired scale for the boxes w.r.t. w and h
        :return:                targets in (x1, y1, x2, y2) format
                                in range [0, w * self.image_scale] [0, h * self.image_scale]
        """
        # unscale
        target_boxes[:, 2:] *= np.array([[w, h, w, h]])

        # x1 = c1 - w // 2; y1 = c2 - h // 2
        target_boxes[:, 2] -= target_boxes[:, 4] // 2
        target_boxes[:, 3] -= target_boxes[:, 5] // 2
        # x2 = w + x1; y2 = h + y1
        target_boxes[:, 4] += target_boxes[:, 2]
        target_boxes[:, 5] += target_boxes[:, 3]

        target_boxes[:, 2:] *= image_scale
        target_boxes = target_boxes.astype(int)
        return target_boxes

    @staticmethod
    def visualize_batch(
        image_tensor: torch.Tensor,
        pred_boxes: List[torch.Tensor],
        target_boxes: torch.Tensor,
        batch_name: Union[int, str],
        class_names: List[str],
        checkpoint_dir: str = None,
        undo_preprocessing_func: Callable[[torch.Tensor], np.ndarray] = undo_image_preprocessing,
        box_thickness: Optional[int] = None,
        image_scale: float = 1.0,
        gt_alpha: float = 0.4,
    ):
        """
        A helper function to visualize detections predicted by a network:
        saves images into a given path with a name that is {batch_name}_{imade_idx_in_the_batch}.jpg, one batch per call.
        Colors are generated on the fly: uniformly sampled from color wheel to support all given classes.

        Adjustable:
            * Ground truth box transparency;
            * Box width;
            * Image size (larger or smaller than what's provided)

        :param image_tensor:            rgb images, (B, H, W, 3)
        :param pred_boxes:              boxes after NMS for each image in a batch, each (Num_boxes, 6),
                                        values on dim 1 are: x1, y1, x2, y2, confidence, class
        :param target_boxes:            (Num_targets, 6), values on dim 1 are: image id in a batch, class, cx cy w h
                                        (coordinates scaled to [0, 1])
        :param batch_name:              id of the current batch to use for image naming

        :param class_names:             names of all classes, each on its own index
        :param checkpoint_dir:          a path where images with boxes will be saved. if None, the result images will
                                        be returns as a list of numpy image arrays

        :param undo_preprocessing_func: a function to convert preprocessed images tensor into a batch of cv2-like images
        :param box_thickness:           box line thickness in px
        :param image_scale:             scale of an image w.r.t. given image size,
                                        e.g. incoming images are (320x320), use scale = 2. to preview in (640x640)
        :param gt_alpha:                a value in [0., 1.] transparency on ground truth boxes,
                                        0 for invisible, 1 for fully opaque
        """
        image_np = undo_preprocessing_func(image_tensor.detach())
        targets = DetectionVisualization._scaled_ccwh_to_xyxy(target_boxes.detach().cpu().numpy().copy(), *image_np.shape[1:3], image_scale)
        if pred_boxes is None:
            pred_boxes = [None for _ in range(image_np.shape[0])]

        out_images = []
        for i in range(image_np.shape[0]):
            preds = pred_boxes[i].detach().cpu().numpy() if pred_boxes[i] is not None else np.empty((0, 6))
            targets_cur = targets[targets[:, 0] == i]

            image_name = "_".join([str(batch_name), str(i)])
            res_image = DetectionVisualization._visualize_image(
                image_np=image_np[i],
                pred_boxes=preds,
                target_boxes=targets_cur,
                class_names=class_names,
                box_thickness=box_thickness,
                gt_alpha=gt_alpha,
                image_scale=image_scale,
                checkpoint_dir=checkpoint_dir,
                image_name=image_name,
            )
            if res_image is not None:
                out_images.append(res_image)

        return out_images


class Anchors:
    """
    A wrapper function to hold the anchors used by detection models such as Yolo
    """

    def __init__(self, anchors_list: List[List], strides: List[int]):
        """
        :param anchors_list: of the shape [[w1,h1,w2,h2,w3,h3], [w4,h4,w5,h5,w6,h6] .... where each sublist holds
            the width and height of the anchors of a specific detection layer.
            i.e. for a model with 3 detection layers, each containing 5 anchors the format will be a of 3 sublists of 10 numbers each
            The width and height are in pixels (not relative to image size)
        :param strides: a list containing the stride of the layers from which the detection heads are fed.
            i.e. if the firs detection head is connected to the backbone after the input dimensions were reduces by 8, the first number will be 8
        """
        super().__init__()

        self.__anchors_list = anchors_list
        self.__strides = tuple(strides)

        self._check_all_lists(anchors_list)
        self._check_all_len_equal_and_even(anchors_list)

        self._stride = np.array(strides, dtype=np.float32)
        anchors = np.array(anchors_list, dtype=np.float32).reshape((len(anchors_list), -1, 2))
        self._anchors = anchors / self._stride.reshape((-1, 1, 1))
        self._anchor_grid = anchors.copy().reshape(len(anchors_list), 1, -1, 1, 1, 2)

    @staticmethod
    def _check_all_lists(anchors: list) -> bool:
        for a in anchors:
            if not isinstance(a, (list, ListConfig)):
                raise RuntimeError("All objects of anchors_list must be lists")

    @staticmethod
    def _check_all_len_equal_and_even(anchors: list) -> bool:
        len_of_first = len(anchors[0])
        for a in anchors:
            if len(a) % 2 == 1 or len(a) != len_of_first:
                raise RuntimeError("All objects of anchors_list must be of the same even length")

    @property
    def stride(self) -> np.ndarray:
        return self._stride

    @property
    def anchors(self) -> np.ndarray:
        return self._anchors

    @property
    def anchor_grid(self) -> np.ndarray:
        return self._anchor_grid

    @property
    def detection_layers_num(self) -> int:
        return self._anchors.shape[0]

    @property
    def num_anchors(self) -> int:
        return self._anchors.shape[1]

    def __repr__(self):
        return f"anchors_list: {self.__anchors_list} strides: {self.__strides}"


def xyxy2cxcywh(bboxes):
    """
    Transforms bboxes from xyxy format to centerized xy wh format
    :param bboxes: array, shaped (nboxes, 4)
    :return: modified bboxes
    """
    bboxes[:, 2] = bboxes[:, 2] - bboxes[:, 0]
    bboxes[:, 3] = bboxes[:, 3] - bboxes[:, 1]
    bboxes[:, 0] = bboxes[:, 0] + bboxes[:, 2] * 0.5
    bboxes[:, 1] = bboxes[:, 1] + bboxes[:, 3] * 0.5
    return bboxes


def cxcywh2xyxy(bboxes):
    """
    Transforms bboxes from centerized xy wh format to xyxy format
    :param bboxes: array, shaped (nboxes, 4)
    :return: modified bboxes
    """
    bboxes[:, 1] = bboxes[:, 1] - bboxes[:, 3] * 0.5
    bboxes[:, 0] = bboxes[:, 0] - bboxes[:, 2] * 0.5
    bboxes[:, 3] = bboxes[:, 3] + bboxes[:, 1]
    bboxes[:, 2] = bboxes[:, 2] + bboxes[:, 0]
    return bboxes


def get_mosaic_coordinate(mosaic_index, xc, yc, w, h, input_h, input_w):
    """
    Returns the mosaic coordinates of final mosaic image according to mosaic image index.

    :param mosaic_index: (int) mosaic image index
    :param xc: (int) center x coordinate of the entire mosaic grid.
    :param yc: (int) center y coordinate of the entire mosaic grid.
    :param w: (int) width of bbox
    :param h: (int) height of bbox
    :param input_h: (int) image input height (should be 1/2 of the final mosaic output image height).
    :param input_w: (int) image input width (should be 1/2 of the final mosaic output image width).
    :return: (x1, y1, x2, y2), (x1s, y1s, x2s, y2s) where (x1, y1, x2, y2) are the coordinates in the final mosaic
        output image, and (x1s, y1s, x2s, y2s) are the coordinates in the placed image.
    """
    # index0 to top left part of image
    if mosaic_index == 0:
        x1, y1, x2, y2 = max(xc - w, 0), max(yc - h, 0), xc, yc
        small_coord = w - (x2 - x1), h - (y2 - y1), w, h
    # index1 to top right part of image
    elif mosaic_index == 1:
        x1, y1, x2, y2 = xc, max(yc - h, 0), min(xc + w, input_w * 2), yc
        small_coord = 0, h - (y2 - y1), min(w, x2 - x1), h
    # index2 to bottom left part of image
    elif mosaic_index == 2:
        x1, y1, x2, y2 = max(xc - w, 0), yc, xc, min(input_h * 2, yc + h)
        small_coord = w - (x2 - x1), 0, w, min(y2 - y1, h)
    # index2 to bottom right part of image
    elif mosaic_index == 3:
        x1, y1, x2, y2 = xc, yc, min(xc + w, input_w * 2), min(input_h * 2, yc + h)  # noqa
        small_coord = 0, 0, min(w, x2 - x1), min(y2 - y1, h)
    return (x1, y1, x2, y2), small_coord


def adjust_box_anns(bbox, scale_ratio, padw, padh, w_max, h_max):
    """
    Adjusts the bbox annotations of rescaled, padded image.

    :param bbox: (np.array) bbox to modify.
    :param scale_ratio: (float) scale ratio between rescale output image and original one.
    :param padw: (int) width padding size.
    :param padh: (int) height padding size.
    :param w_max: (int) width border.
    :param h_max: (int) height border
    :return: modified bbox (np.array)
    """
    scaled_bboxes = bbox * scale_ratio + np.array([[padw, padh, padw, padh]])
    return change_bbox_bounds_for_image_size_inplace(scaled_bboxes, img_shape=(h_max, w_max))


def compute_box_area(box: torch.Tensor) -> torch.Tensor:
    """
    Compute the area of one or many boxes.
    :param box: One or many boxes, shape = (4, ?), each box in format (x1, y1, x2, y2)
    :return: Area of every box, shape = (1, ?)
    """
    # box = 4xn
    return (box[2] - box[0]) * (box[3] - box[1])


def crowd_ioa(det_box: torch.Tensor, crowd_box: torch.Tensor) -> torch.Tensor:
    """
    Return intersection-over-detection_area of boxes, used for crowd ground truths.
    Both sets of boxes are expected to be in (x1, y1, x2, y2) format.

    :param det_box:     Tensor of shape [N, 4]
    :param crowd_box:   Tensor of shape [M, 4]
    :return: crowd_ioa, Tensor of shape [N, M]: the NxM matrix containing the pairwise IoA values for every element in det_box and crowd_box
    """
    det_area = compute_box_area(det_box.T)

    # inter(N,M) = (rb(N,M,2) - lt(N,M,2)).clamp(0).prod(2)
    inter = (torch.min(det_box[:, None, 2:], crowd_box[:, 2:]) - torch.max(det_box[:, None, :2], crowd_box[:, :2])).clamp(0).prod(2)
    return inter / det_area[:, None]  # crowd_ioa = inter / det_area


class DetectionMatching(ABC):
    """
    DetectionMatching is an abstract base class that defines the interface for matching detections
    in object detection models. It includes methods for computing targets for both regular and crowd
    scenarios, as well as getting thresholds for matching.
    """

    @abstractmethod
    def get_thresholds(self) -> torch.Tensor:
        """
        Abstract method to get the thresholds used for detection matching.

        :return: (torch.Tensor) The thresholds used in the matching process.
        """
        pass

    @abstractmethod
    def compute_targets(
        self,
        preds_box_xyxy: torch.Tensor,
        preds_cls: torch.Tensor,
        targets_box_xyxy: torch.Tensor,
        targets_cls: torch.Tensor,
        preds_matched: torch.Tensor,
        targets_matched: torch.Tensor,
        preds_idx_to_use: torch.Tensor,
    ) -> torch.Tensor:
        """
        Abstract method to compute targets for regular scenarios.

        :param preds_box_xyxy: (torch.Tensor) Predicted bounding boxes in XYXY format.
        :param preds_cls: (torch.Tensor) Predicted classes.
        :param targets_box_xyxy: (torch.Tensor) Target bounding boxes in XYXY format.
        :param targets_cls: (torch.Tensor) Target classes.
        :param preds_matched: (torch.Tensor) Tensor indicating which predictions are matched.
        :param targets_matched: (torch.Tensor) Tensor indicating which targets are matched.
        :param preds_idx_to_use: (torch.Tensor) Indices of predictions to use.
        :return: (torch.Tensor) Computed targets.
        """
        pass

    @abstractmethod
    def compute_crowd_targets(
        self,
        preds_box_xyxy: torch.Tensor,
        preds_cls: torch.Tensor,
        crowd_targets_cls: torch.Tensor,
        crowd_target_box_xyxy: torch.Tensor,
        preds_matched: torch.Tensor,
        preds_to_ignore: torch.Tensor,
        preds_idx_to_use: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Abstract method to compute targets for crowd scenarios.

        :param preds_box_xyxy: (torch.Tensor) Predicted bounding boxes in XYXY format.
        :param preds_cls: (torch.Tensor) Predicted classes.
        :param crowd_targets_cls: (torch.Tensor) Crowd target classes.
        :param crowd_target_box_xyxy: (torch.Tensor) Crowd target bounding boxes in XYXY format.
        :param preds_matched: (torch.Tensor) Tensor indicating which predictions are matched.
        :param preds_to_ignore: (torch.Tensor) Tensor indicating which predictions to ignore.
        :param preds_idx_to_use: (torch.Tensor) Indices of predictions to use.
        :return: (Tuple[torch.Tensor, torch.Tensor]) Computed targets for crowd scenarios.
        """
        pass


class IoUMatching(DetectionMatching):
    """
    IoUMatching is a subclass of DetectionMatching that uses Intersection over Union (IoU)
    for matching detections in object detection models.
    """

    def __init__(self, iou_thresholds: torch.Tensor):
        """
        Initializes the IoUMatching instance with IoU thresholds.

        :param iou_thresholds: (torch.Tensor) The IoU thresholds for matching.
        """
        self.iou_thresholds = iou_thresholds

    def get_thresholds(self) -> torch.Tensor:
        """
        Returns the IoU thresholds used for detection matching.

        :return: (torch.Tensor) The IoU thresholds.
        """
        return self.iou_thresholds

    def compute_targets(
        self,
        preds_box_xyxy: torch.Tensor,
        preds_cls: torch.Tensor,
        targets_box_xyxy: torch.Tensor,
        targets_cls: torch.Tensor,
        preds_matched: torch.Tensor,
        targets_matched: torch.Tensor,
        preds_idx_to_use: torch.Tensor,
    ) -> torch.Tensor:
        """
        Computes the matching targets based on IoU for regular scenarios.

        :param preds_box_xyxy: (torch.Tensor) Predicted bounding boxes in XYXY format.
        :param preds_cls: (torch.Tensor) Predicted classes.
        :param targets_box_xyxy: (torch.Tensor) Target bounding boxes in XYXY format.
        :param targets_cls: (torch.Tensor) Target classes.
        :param preds_matched: (torch.Tensor) Tensor indicating which predictions are matched.
        :param targets_matched: (torch.Tensor) Tensor indicating which targets are matched.
        :param preds_idx_to_use: (torch.Tensor) Indices of predictions to use.
        :return: (torch.Tensor) Computed matching targets.
        """
        # shape = (n_preds x n_targets)
        iou = box_iou(preds_box_xyxy[preds_idx_to_use], targets_box_xyxy)

        # Fill IoU values at index (i, j) with 0 when the prediction (i) and target(j) are of different class
        # Filling with 0 is equivalent to ignore these values since with want IoU > iou_threshold > 0
        cls_mismatch = preds_cls[preds_idx_to_use].view(-1, 1) != targets_cls.view(1, -1)
        iou[cls_mismatch] = 0

        # The matching priority is first detection confidence and then IoU value.
        # The detection is already sorted by confidence in NMS, so here for each prediction we order the targets by iou.
        sorted_iou, target_sorted = iou.sort(descending=True, stable=True)

        # Only iterate over IoU values higher than min threshold to speed up the process
        for pred_selected_i, target_sorted_i in (sorted_iou > self.iou_thresholds[0]).nonzero(as_tuple=False):
            # pred_selected_i and target_sorted_i are relative to filters/sorting, so we extract their absolute indexes
            pred_i = preds_idx_to_use[pred_selected_i]
            target_i = target_sorted[pred_selected_i, target_sorted_i]

            # Vector[j], True when IoU(pred_i, target_i) is above the (j)th threshold
            is_iou_above_threshold = sorted_iou[pred_selected_i, target_sorted_i] > self.iou_thresholds

            # Vector[j], True when both pred_i and target_i are not matched yet for the (j)th threshold
            are_candidates_free = torch.logical_and(~preds_matched[pred_i, :], ~targets_matched[target_i, :])

            # Vector[j], True when (pred_i, target_i) can be matched for the (j)th threshold
            are_candidates_good = torch.logical_and(is_iou_above_threshold, are_candidates_free)

            # For every threshold (j) where target_i and pred_i can be matched together ( are_candidates_good[j]==True )
            # fill the matching placeholders with True
            targets_matched[target_i, are_candidates_good] = True
            preds_matched[pred_i, are_candidates_good] = True

            # When all the targets are matched with a prediction for every IoU Threshold, stop.
            if targets_matched.all():
                break

        return preds_matched

    def compute_crowd_targets(
        self,
        preds_box_xyxy: torch.Tensor,
        preds_cls: torch.Tensor,
        crowd_targets_cls: torch.Tensor,
        crowd_target_box_xyxy: torch.Tensor,
        preds_matched: torch.Tensor,
        preds_to_ignore: torch.Tensor,
        preds_idx_to_use: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Computes the matching targets based on IoU for crowd scenarios.

        :param preds_box_xyxy: (torch.Tensor) Predicted bounding boxes in XYXY format.
        :param preds_cls: (torch.Tensor) Predicted classes.
        :param crowd_targets_cls: (torch.Tensor) Crowd target classes.
        :param crowd_target_box_xyxy: (torch.Tensor) Crowd target bounding boxes in XYXY format.
        :param preds_matched: (torch.Tensor) Tensor indicating which predictions are matched.
        :param preds_to_ignore: (torch.Tensor) Tensor indicating which predictions to ignore.
        :param preds_idx_to_use: (torch.Tensor) Indices of predictions to use.
        :return: (Tuple[torch.Tensor, torch.Tensor]) Computed matching targets for crowd scenarios.
        """
        # Crowd targets can be matched with many predictions.
        # Therefore, for every prediction we just need to check if it has IoA large enough with any crowd target.

        # shape = (n_preds_to_use x n_crowd_targets)
        ioa = crowd_ioa(preds_box_xyxy[preds_idx_to_use], crowd_target_box_xyxy)

        # Fill IoA values at index (i, j) with 0 when the prediction (i) and target(j) are of different class
        # Filling with 0 is equivalent to ignore these values since with want IoA > threshold > 0
        cls_mismatch = preds_cls[preds_idx_to_use].view(-1, 1) != crowd_targets_cls.view(1, -1)
        ioa[cls_mismatch] = 0

        # For each prediction, we keep it's highest score with any crowd target (of same class)
        # shape = (n_preds_to_use)
        best_ioa, _ = ioa.max(1)

        # If a prediction has IoA higher than threshold (with any target of same class), then there is a match
        # shape = (n_preds_to_use x iou_thresholds)
        is_matching_with_crowd = best_ioa.view(-1, 1) > self.iou_thresholds.view(1, -1)

        preds_to_ignore[preds_idx_to_use] = torch.logical_or(preds_to_ignore[preds_idx_to_use], is_matching_with_crowd)

        return preds_matched, preds_to_ignore


class DistanceMatching(DetectionMatching):
    """
    DistanceMatching is a subclass of DetectionMatching that uses a distance metric
    for matching detections in object detection models.
    """

    def __init__(self, distance_metric, distance_thresholds: torch.Tensor):
        """
        Initializes the DistanceMatching instance with a distance metric and distance thresholds.

        :param distance_metric: The distance metric to be used for matching.
        :param distance_thresholds: (torch.Tensor) The distance thresholds for matching.
        """
        self.distance_metric = distance_metric
        self.distance_thresholds = distance_thresholds

    def get_thresholds(self) -> torch.Tensor:
        """
        Returns the distance thresholds used for detection matching.

        :return: (torch.Tensor) The distance thresholds.
        """
        return torch.tensor(self.distance_thresholds)

    def compute_targets(
        self,
        preds_box_xyxy: torch.Tensor,
        preds_cls: torch.Tensor,
        targets_box_xyxy: torch.Tensor,
        targets_cls: torch.Tensor,
        preds_matched: torch.Tensor,
        targets_matched: torch.Tensor,
        preds_idx_to_use: torch.Tensor,
    ) -> torch.Tensor:
        """
        Computes the matching targets based on the distance metric for regular scenarios.

        :param preds_box_xyxy: (torch.Tensor) Predicted bounding boxes in XYXY format.
        :param preds_cls: (torch.Tensor) Predicted classes.
        :param targets_box_xyxy: (torch.Tensor) Target bounding boxes in XYXY format.
        :param targets_cls: (torch.Tensor) Target classes.
        :param preds_matched: (torch.Tensor) Tensor indicating which predictions are matched.
        :param targets_matched: (torch.Tensor) Tensor indicating which targets are matched.
        :param preds_idx_to_use: (torch.Tensor) Indices of predictions to use.
        :return: (torch.Tensor) Computed matching targets.
        """
        # Calculate the distances between targets and predictions using the current metric
        # shape = (n_preds x n_targets)
        distances = self.distance_metric.calculate_distance(preds_box_xyxy[preds_idx_to_use], targets_box_xyxy)

        # Invalidate distances when class labels don't match
        cls_mismatch = preds_cls[preds_idx_to_use].view(-1, 1) != targets_cls.view(1, -1)
        distances[cls_mismatch] = float("inf")  # or max(distance_thresholds) + 1

        # Sort distances
        sorted_distances, target_sorted = distances.sort(stable=True)

        # Identify all pairs that are within the max distance threshold
        candidate_pairs = (sorted_distances < max(self.distance_thresholds)).nonzero(as_tuple=False)
        for pred_selected_i, target_sorted_i in candidate_pairs:
            pred_i = preds_idx_to_use[pred_selected_i]
            target_i = target_sorted[pred_selected_i, target_sorted_i]

            distance_thresholds_tensor = torch.tensor(self.distance_thresholds, device=distances.device)
            is_distance_below_threshold = sorted_distances[pred_selected_i, target_sorted_i] < distance_thresholds_tensor
            are_candidates_free = torch.logical_and(~preds_matched[pred_i, :], ~targets_matched[target_i, :])
            are_candidates_good = torch.logical_and(is_distance_below_threshold, are_candidates_free)

            targets_matched[target_i, are_candidates_good] = True
            preds_matched[pred_i, are_candidates_good] = True

            if targets_matched.all():
                break

        return preds_matched

    def compute_crowd_targets(
        self,
        preds_box_xyxy: torch.Tensor,
        preds_cls: torch.Tensor,
        crowd_targets_cls: torch.Tensor,
        crowd_target_box_xyxy: torch.Tensor,
        preds_matched: torch.Tensor,
        preds_to_ignore: torch.Tensor,
        preds_idx_to_use: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Computes the matching targets based on the distance metric for crowd scenarios.

        :param preds_box_xyxy: (torch.Tensor) Predicted bounding boxes in XYXY format.
        :param preds_cls: (torch.Tensor) Predicted classes.
        :param crowd_targets_cls: (torch.Tensor) Crowd target classes.
        :param crowd_target_box_xyxy: (torch.Tensor) Crowd target bounding boxes in XYXY format.
        :param preds_matched: (torch.Tensor) Tensor indicating which predictions are matched.
        :param preds_to_ignore: (torch.Tensor) Tensor indicating which predictions to ignore.
        :param preds_idx_to_use: (torch.Tensor) Indices of predictions to use.
        :return: (Tuple[torch.Tensor, torch.Tensor]) Computed matching targets for crowd scenarios.
        """
        cls_mismatch_crowd = preds_cls[preds_idx_to_use].view(-1, 1) != crowd_targets_cls.view(1, -1)

        # Iterate over each distance metric and its corresponding threshold
        distances = self.distance_metric.calculate_distance(preds_box_xyxy[preds_idx_to_use], crowd_target_box_xyxy)
        distances[cls_mismatch_crowd] = float("inf")

        best_distance, _ = distances.min(1)
        is_matching_with_crowd = best_distance.view(-1, 1) < torch.tensor(self.distance_thresholds, device=distances.device).view(1, -1)

        preds_to_ignore[preds_idx_to_use] = torch.logical_or(preds_to_ignore[preds_idx_to_use], is_matching_with_crowd)

        return preds_matched, preds_to_ignore


def compute_detection_matching(
    output: List[torch.Tensor],
    targets: torch.Tensor,
    height: int,
    width: int,
    denormalize_targets: bool,
    device: str,
    iou_thresholds: torch.Tensor = None,
    crowd_targets: Optional[torch.Tensor] = None,
    top_k: int = 100,
    return_on_cpu: bool = True,
    matching_strategy: DetectionMatching = None,
) -> List[Tuple]:
    """
    Match predictions (NMS output) and the targets (ground truth) with respect to IoU and confidence score.
    :param output:          list (of length batch_size) of Tensors of shape (num_predictions, 6)
                            format:     (x1, y1, x2, y2, confidence, class_label) where x1,y1,x2,y2 are according to image size
    :param targets:         targets for all images of shape (total_num_targets, 6)
                            format:     (index, label, x, y, w, h, ) where x,y,w,h are in range [0,1]
    :param height:          dimensions of the image
    :param width:           dimensions of the image
    :param iou_thresholds:  Threshold to compute the mAP
    :param device:          Device
    :param crowd_targets:   crowd targets for all images of shape (total_num_crowd_targets, 6)
                            format:     (index, label, x, y, w, h) where x,y,w,h are in range [0,1]
    :param top_k:           Number of predictions to keep per class, ordered by confidence score
    :param denormalize_targets: If True, denormalize the targets and crowd_targets
    :param return_on_cpu:   If True, the output will be returned on "CPU", otherwise it will be returned on "device"
    :param matching_strategy: Method to match predictions to ground truth targets, IoU, distance based

    :return:                list of the following tensors, for every image:
        :preds_matched:     Tensor of shape (num_img_predictions, n_thresholds)
                            True when prediction (i) is matched with a target with respect to the (j)th IoU threshold
        :preds_to_ignore:   Tensor of shape (num_img_predictions, n_thresholds)
                            True when prediction (i) is matched with a crowd target with respect to the (j)th IoU threshold
        :preds_scores:      Tensor of shape (num_img_predictions), confidence score for every prediction
        :preds_cls:         Tensor of shape (num_img_predictions), predicted class for every prediction
        :targets_cls:       Tensor of shape (num_img_targets), ground truth class for every target
    """
    if matching_strategy is None:
        raise ValueError("matching_strategy must not be None")
    if isinstance(matching_strategy, IoUMatching) and iou_thresholds is None:
        raise ValueError("iou_thresholds is required for IoU matching strategy")

    output = map(lambda tensor: None if tensor is None else tensor.to(device), output)
    thresholds = matching_strategy.get_thresholds()
    targets, thresholds = targets.to(device), thresholds.to(device)

    # If crowd_targets is not provided, we patch it with an empty tensor
    crowd_targets = torch.zeros(size=(0, 6), device=device) if crowd_targets is None else crowd_targets.to(device)

    batch_metrics = []
    for img_i, img_preds in enumerate(output):
        # If img_preds is None (not prediction for this image), we patch it with an empty tensor
        img_preds = img_preds if img_preds is not None else torch.zeros(size=(0, 6), device=device)
        img_targets = targets[targets[:, 0] == img_i, 1:]
        img_crowd_targets = crowd_targets[crowd_targets[:, 0] == img_i, 1:]

        img_matching_tensors = compute_img_detection_matching(
            preds=img_preds,
            targets=img_targets,
            crowd_targets=img_crowd_targets,
            denormalize_targets=denormalize_targets,
            height=height,
            width=width,
            iou_thresholds=iou_thresholds,
            device=device,
            top_k=top_k,
            return_on_cpu=return_on_cpu,
            matching_strategy=matching_strategy,
        )
        batch_metrics.append(img_matching_tensors)

    return batch_metrics


def compute_img_detection_matching(
    preds: torch.Tensor,
    targets: torch.Tensor,
    crowd_targets: torch.Tensor,
    height: int,
    width: int,
    device: str,
    denormalize_targets: bool,
    iou_thresholds: torch.Tensor = None,
    top_k: int = 100,
    return_on_cpu: bool = True,
    matching_strategy: DetectionMatching = None,
) -> Tuple:
    """
    Match predictions (NMS output) and the targets (ground truth) with respect to metric and confidence score
    for a given image.
    :param preds:           Tensor of shape (num_img_predictions, 6)
                            format:     (x1, y1, x2, y2, confidence, class_label) where x1,y1,x2,y2 are according to image size
    :param targets:         targets for this image of shape (num_img_targets, 6)
                            format:     (label, cx, cy, w, h) where cx,cy,w,h
    :param height:          dimensions of the image
    :param width:           dimensions of the image
    :param device:
    :param crowd_targets:   crowd targets for all images of shape (total_num_crowd_targets, 6)
                            format:     (index, x, y, w, h) where x,y,w,h are in range [0,1]
    :param iou_thresholds:  Threshold to compute the mAP
    :param top_k:           Number of predictions to keep per class, ordered by confidence score
    :param device:          Device
    :param denormalize_targets: If True, denormalize the targets and crowd_targets
    :param return_on_cpu:   If True, the output will be returned on "CPU", otherwise it will be returned on "device"
    :param matching_strategy: Method to match predictions to ground truth targets: IoU, distance based

    :return:
        :preds_matched:     Tensor of shape (num_img_predictions, n_thresholds)
                                True when prediction (i) is matched with a target with respect to the (j)th threshold
        :preds_to_ignore:   Tensor of shape (num_img_predictions, n_thresholds)
                                True when prediction (i) is matched with a crowd target with respect to the (j)th threshold
        :preds_scores:      Tensor of shape (num_img_predictions), confidence score for every prediction
        :preds_cls:         Tensor of shape (num_img_predictions), predicted class for every prediction
        :targets_cls:       Tensor of shape (num_img_targets), ground truth class for every target
    """
    num_thresholds = len(matching_strategy.get_thresholds())

    if preds is None or len(preds) == 0:
        if return_on_cpu:
            device = "cpu"
        preds_matched = torch.zeros((0, num_thresholds), dtype=torch.bool, device=device)
        preds_to_ignore = torch.zeros((0, num_thresholds), dtype=torch.bool, device=device)
        preds_scores = torch.tensor([], dtype=torch.float32, device=device)
        preds_cls = torch.tensor([], dtype=torch.float32, device=device)
        targets_cls = targets[:, 0].to(device=device)
        return preds_matched, preds_to_ignore, preds_scores, preds_cls, targets_cls

    preds_matched = torch.zeros(len(preds), num_thresholds, dtype=torch.bool, device=preds.device)
    targets_matched = torch.zeros(len(targets), num_thresholds, dtype=torch.bool, device=preds.device)
    preds_to_ignore = torch.zeros(len(preds), num_thresholds, dtype=torch.bool, device=preds.device)

    preds_cls, preds_box, preds_scores = preds[:, -1], preds[:, 0:4], preds[:, 4]
    targets_cls, targets_box = targets[:, 0], targets[:, 1:5]
    crowd_targets_cls, crowd_target_box = crowd_targets[:, 0], crowd_targets[:, 1:5]

    # Ignore all but the predictions that were top_k for their class
    preds_idx_to_use = get_top_k_idx_per_cls(preds_scores, preds_cls, top_k)
    preds_to_ignore[:, :] = True
    preds_to_ignore[preds_idx_to_use] = False

    if len(targets) > 0 or len(crowd_targets) > 0:
        # CHANGE bboxes TO FIT THE IMAGE SIZE
        change_bbox_bounds_for_image_size_inplace(preds, (height, width))

        targets_box = cxcywh2xyxy(targets_box)
        crowd_target_box = cxcywh2xyxy(crowd_target_box)

        if denormalize_targets:
            targets_box[:, [0, 2]] *= width
            targets_box[:, [1, 3]] *= height
            crowd_target_box[:, [0, 2]] *= width
            crowd_target_box[:, [1, 3]] *= height

        if len(targets) > 0:
            preds_matched = matching_strategy.compute_targets(preds_box, preds_cls, targets_box, targets_cls, preds_matched, targets_matched, preds_idx_to_use)

        if len(crowd_targets) > 0:
            preds_matched, preds_to_ignore = matching_strategy.compute_crowd_targets(
                preds_box, preds_cls, crowd_targets_cls, crowd_target_box, preds_matched, preds_to_ignore, preds_idx_to_use
            )

    if return_on_cpu:
        preds_matched = preds_matched.to("cpu")
        preds_to_ignore = preds_to_ignore.to("cpu")
        preds_scores = preds_scores.to("cpu")
        preds_cls = preds_cls.to("cpu")
        targets_cls = targets_cls.to("cpu")

    return preds_matched, preds_to_ignore, preds_scores, preds_cls, targets_cls


class DistanceMetric(ABC):
    @abstractmethod
    def calculate_distance(self, preds_box: torch.Tensor, targets_box: torch.Tensor) -> torch.Tensor:
        pass


class EuclideanDistance(DistanceMetric):
    def calculate_distance(self, predicted: torch.Tensor, target: torch.Tensor):
        """
        Calculate the Euclidean distance (L2 distance) between the centers of preds_box and targets_box.

        :param predicted: (N, 4) tensor for N predicted bounding boxes (x1, y1, x2, y2)
        :param target: (M, 4) tensor for M target bounding boxes (x1, y1, x2, y2)

        :return: (N, M) tensor representing pairwise euclidean distances
        """
        # Calculate the centers of the bounding boxes
        centers1 = (predicted[:, :2] + predicted[:, 2:]) / 2
        centers2 = (target[:, :2] + target[:, 2:]) / 2

        # Calculate squared differences
        diff = centers1.view(-1, 1, 2) - centers2.view(1, -1, 2)
        dist_sq = (diff**2).sum(dim=2)
        dist = torch.sqrt(dist_sq)

        return dist


class ManhattanDistance(DistanceMetric):
    def calculate_distance(self, predicted: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        """
        Calculate the Manhattan distance (L1 distance) between the centers of preds_box and targets_box.

        :param predicted: (N, 4) tensor for N predicted bounding boxes (x1, y1, x2, y2)
        :param target: (M, 4) tensor for M target bounding boxes (x1, y1, x2, y2)

        :return: (N, M) tensor representing pairwise Manhattan distances
        """
        # Calculate the centers of the bounding boxes
        centers1 = (predicted[:, :2] + predicted[:, 2:]) / 2  # (N, 2)
        centers2 = (target[:, :2] + target[:, 2:]) / 2  # (M, 2)

        # Calculate absolute differences
        diff = centers1.view(-1, 1, 2) - centers2.view(1, -1, 2)
        abs_diff = torch.abs(diff).sum(dim=2)

        return abs_diff


def get_top_k_idx_per_cls(preds_scores: torch.Tensor, preds_cls: torch.Tensor, top_k: int):
    """Get the indexes of all the top k predictions for every class

    :param preds_scores:   The confidence scores, vector of shape (n_pred)
    :param preds_cls:      The predicted class, vector of shape (n_pred)
    :param top_k:          Number of predictions to keep per class, ordered by confidence score

    :return top_k_idx:     Indexes of the top k predictions. length <= (k * n_unique_class)
    """
    n_unique_cls = torch.max(preds_cls)
    mask = preds_cls.view(-1, 1) == torch.arange(n_unique_cls + 1, device=preds_scores.device).view(1, -1)
    preds_scores_per_cls = preds_scores.view(-1, 1) * mask

    sorted_scores_per_cls, sorting_idx = preds_scores_per_cls.sort(0, descending=True)
    idx_with_satisfying_scores = sorted_scores_per_cls[:top_k, :].nonzero(as_tuple=False)
    top_k_idx = sorting_idx[idx_with_satisfying_scores.split(1, dim=1)]
    return top_k_idx.view(-1)


def compute_detection_metrics(
    preds_matched: torch.Tensor,
    preds_to_ignore: torch.Tensor,
    preds_scores: torch.Tensor,
    preds_cls: torch.Tensor,
    targets_cls: torch.Tensor,
    device: str,
    recall_thresholds: Optional[torch.Tensor] = None,
    score_threshold: Optional[float] = 0.1,
    calc_best_score_thresholds: bool = None,
) -> Tuple:
    """
    Compute the list of precision, recall, MaP and f1 for every recall IoU threshold and for every class.

    :param preds_matched:      Tensor of shape (num_predictions, n_iou_thresholds)
                                    True when prediction (i) is matched with a target with respect to the (j)th IoU threshold
    :param preds_to_ignore     Tensor of shape (num_predictions, n_iou_thresholds)
                                    True when prediction (i) is matched with a crowd target with respect to the (j)th IoU threshold
    :param preds_scores:       Tensor of shape (num_predictions), confidence score for every prediction
    :param preds_cls:          Tensor of shape (num_predictions), predicted class for every prediction
    :param targets_cls:        Tensor of shape (num_targets), ground truth class for every target box to be detected
    :param recall_thresholds:   Recall thresholds used to compute MaP.
    :param score_threshold:    Minimum confidence score to consider a prediction for the computation of
                                    precision, recall and f1 (not MaP)
    :param device:             Device
    :param calc_best_score_thresholds: (Deprecated) If True, the best confidence score threshold is computed for each class
                                       This parameter is deprecated and ignore. Function always compute best threshold.

    :return:
        :ap, precision, recall, f1: Tensors of shape (n_class, nb_iou_thrs)
        :unique_classes:            Vector with all unique target classes
        :best_score_threshold:      torch.float with the best overall score threshold if calc_best_score_thresholds
                                    is True else None
        :best_score_threshold_per_cls: Array that stores the best score threshold for each class , if
                                            calc_best_score_thresholds is True else None

    """
    if calc_best_score_thresholds is not None:
        warnings.warn(
            "calc_best_score_thresholds argument is deprecated and will be removed in SG 3.8.0.\n"
            "Best score threhsold is always computed by compute_detection_metrics since SG 3.6.0.\n"
            "Please update your code and remove explicitely passing calc_best_score_thresholds.\n"
        )

    preds_matched, preds_to_ignore = preds_matched.to(device), preds_to_ignore.to(device)
    preds_scores, preds_cls, targets_cls = preds_scores.to(device), preds_cls.to(device), targets_cls.to(device)

    recall_thresholds = torch.linspace(0, 1, 101, device=device) if recall_thresholds is None else recall_thresholds.to(device)

    unique_classes = torch.unique(targets_cls).long()

    n_class, nb_iou_thrs = len(unique_classes), preds_matched.shape[-1]

    ap = torch.zeros((n_class, nb_iou_thrs), device=device)
    precision = torch.zeros((n_class, nb_iou_thrs), device=device)
    recall = torch.zeros((n_class, nb_iou_thrs), device=device)

    nb_score_thrs = len(recall_thresholds)
    all_score_thresholds = torch.linspace(0, 1, nb_score_thrs, device=device)
    f1_per_class_per_threshold = torch.zeros((n_class, nb_score_thrs), device=device)
    best_score_threshold_per_cls = torch.zeros(n_class, device=device)

    for cls_i, class_value in enumerate(unique_classes):
        cls_preds_idx, cls_targets_idx = (preds_cls == class_value), (targets_cls == class_value)
        cls_ap, cls_precision, cls_recall, cls_f1_per_threshold, cls_best_score_threshold = compute_detection_metrics_per_cls(
            preds_matched=preds_matched[cls_preds_idx],
            preds_to_ignore=preds_to_ignore[cls_preds_idx],
            preds_scores=preds_scores[cls_preds_idx],
            n_targets=cls_targets_idx.sum(),
            recall_thresholds=recall_thresholds,
            score_threshold=score_threshold,
            device=device,
        )
        ap[cls_i, :] = cls_ap
        precision[cls_i, :] = cls_precision
        recall[cls_i, :] = cls_recall

        f1_per_class_per_threshold[cls_i, :] = cls_f1_per_threshold
        best_score_threshold_per_cls[cls_i] = cls_best_score_threshold

    f1 = 2 * precision * recall / (precision + recall + 1e-16)

    mean_f1_across_classes = torch.mean(f1_per_class_per_threshold, dim=0)
    best_score_threshold = all_score_thresholds[torch.argmax(mean_f1_across_classes)]

    return ap, precision, recall, f1, unique_classes, best_score_threshold, best_score_threshold_per_cls


def compute_detection_metrics_per_cls(
    preds_matched: torch.Tensor,
    preds_to_ignore: torch.Tensor,
    preds_scores: torch.Tensor,
    n_targets: int,
    recall_thresholds: torch.Tensor,
    score_threshold: float,
    device: str,
    calc_best_score_thresholds=None,
):
    """
    Compute the list of precision, recall and MaP of a given class for every recall threshold.

    :param preds_matched:      Tensor of shape (num_predictions, n_thresholds)
                                    True when prediction (i) is matched with a target
                                    with respect to the(j)th threshold
    :param preds_to_ignore     Tensor of shape (num_predictions, n_thresholds)
                                    True when prediction (i) is matched with a crowd target
                                    with respect to the (j)th threshold
    :param preds_scores:       Tensor of shape (num_predictions), confidence score for every prediction
    :param n_targets:          Number of target boxes of this class
    :param recall_thresholds:  Tensor of shape (max_n_rec_thresh) list of recall thresholds used to compute MaP
    :param score_threshold:    Minimum confidence score to consider a prediction for the computation of
                                    precision and recall (not MaP)
    :param device:             Device
    :param nb_score_thrs:              Number of score thresholds to consider when calc_best_score_thresholds is True
    :param calc_best_score_thresholds: (Deprecated) If True, the best confidence score threshold is computed for each class
                                       This parameter is deprecated and ignore. Function always compute best threshold.
    :return:
        :ap, precision, recall:     Tensors of shape (nb_thrs)
        :mean_f1_per_threshold:     Tensor of shape (nb_score_thresholds) if calc_best_score_thresholds is True else None
        :best_score_threshold:      torch.float if calc_best_score_thresholds is True else None
    """
    if calc_best_score_thresholds is not None:
        warnings.warn(
            "calc_best_score_thresholds argument is deprecated and will be removed in SG 3.8.0.\n"
            "Best score threhsold is always computed by compute_detection_metrics since SG 3.6.0.\n"
            "Please update your code and remove explicitely passing calc_best_score_thresholds.\n"
        )

    nb_iou_thrs = preds_matched.shape[-1]
    nb_score_thrs = len(recall_thresholds)

    mean_f1_per_threshold = torch.zeros(nb_score_thrs, device=device)
    best_score_threshold = torch.tensor(0.0, dtype=torch.float, device=device)

    tps = preds_matched
    fps = torch.logical_and(torch.logical_not(preds_matched), torch.logical_not(preds_to_ignore))

    if len(tps) == 0:
        return (
            torch.zeros(nb_iou_thrs, device=device),
            torch.zeros(nb_iou_thrs, device=device),
            torch.zeros(nb_iou_thrs, device=device),
            mean_f1_per_threshold,
            best_score_threshold,
        )

    # Sort by decreasing score
    dtype = torch.uint8 if preds_scores.is_cuda and preds_scores.dtype is torch.bool else preds_scores.dtype
    sort_ind = torch.argsort(preds_scores.to(dtype), descending=True)
    tps = tps[sort_ind, :]
    fps = fps[sort_ind, :]
    preds_scores = preds_scores[sort_ind].contiguous()

    # Rolling sum over the predictions
    rolling_tps = torch.cumsum(tps, axis=0, dtype=torch.float)
    rolling_fps = torch.cumsum(fps, axis=0, dtype=torch.float)

    rolling_recalls = rolling_tps / n_targets
    rolling_precisions = rolling_tps / (rolling_tps + rolling_fps + torch.finfo(torch.float64).eps)

    # Reversed cummax to only have decreasing values
    rolling_precisions = rolling_precisions.flip(0).cummax(0).values.flip(0)

    # ==================
    # RECALL & PRECISION

    # We want the rolling precision/recall at index i so that: preds_scores[i-1] >= score_threshold > preds_scores[i]
    # Note: torch.searchsorted works on increasing sequence and preds_scores is decreasing, so we work with "-"
    # Note2: right=True due to negation
    lowest_score_above_threshold = torch.searchsorted(-preds_scores, -score_threshold, right=True)

    if lowest_score_above_threshold == 0:  # Here score_threshold > preds_scores[0], so no pred is above the threshold
        recall = torch.zeros(nb_iou_thrs, device=device)
        precision = torch.zeros(nb_iou_thrs, device=device)  # the precision is not really defined when no pred but we need to give it a value
    else:
        recall = rolling_recalls[lowest_score_above_threshold - 1]
        precision = rolling_precisions[lowest_score_above_threshold - 1]

    # ==================
    # BEST CONFIDENCE SCORE THRESHOLD PER CLASS
    all_score_thresholds = torch.linspace(0, 1, nb_score_thrs, device=device)

    # We want the rolling precision/recall at index i so that: preds_scores[i-1] > score_threshold >= preds_scores[i]
    # Note: torch.searchsorted works on increasing sequence and preds_scores is decreasing, so we work with "-"
    lowest_scores_above_thresholds = torch.searchsorted(-preds_scores, -all_score_thresholds, right=True)

    # When score_threshold > preds_scores[0], then no pred is above the threshold, so we pad with zeros
    rolling_recalls_padded = torch.cat((torch.zeros(1, nb_iou_thrs, device=device), rolling_recalls), dim=0)
    rolling_precisions_padded = torch.cat((torch.zeros(1, nb_iou_thrs, device=device), rolling_precisions), dim=0)

    # shape = (n_score_thresholds, nb_iou_thrs)
    recalls_per_threshold = torch.index_select(input=rolling_recalls_padded, dim=0, index=lowest_scores_above_thresholds)
    precisions_per_threshold = torch.index_select(input=rolling_precisions_padded, dim=0, index=lowest_scores_above_thresholds)

    # shape (n_score_thresholds, nb_iou_thrs)
    f1_per_threshold = 2 * recalls_per_threshold * precisions_per_threshold / (recalls_per_threshold + precisions_per_threshold + 1e-16)
    mean_f1_per_threshold = torch.mean(f1_per_threshold, dim=1)  # average over iou thresholds
    best_score_threshold = all_score_thresholds[torch.argmax(mean_f1_per_threshold)]

    # ==================
    # AVERAGE PRECISION

    # shape = (nb_iou_thrs, n_recall_thresholds)
    recall_thresholds = recall_thresholds.view(1, -1).repeat(nb_iou_thrs, 1)

    # We want the index i so that: rolling_recalls[i-1] < recall_thresholds[k] <= rolling_recalls[i]
    # Note:  when recall_thresholds[k] > max(rolling_recalls), i = len(rolling_recalls)
    # Note2: we work with transpose (.T) to apply torch.searchsorted on first dim instead of the last one
    recall_threshold_idx = torch.searchsorted(rolling_recalls.T.contiguous(), recall_thresholds, right=False).T

    # When recall_thresholds[k] > max(rolling_recalls), rolling_precisions[i] is not defined, and we want precision = 0
    rolling_precisions = torch.cat((rolling_precisions, torch.zeros(1, nb_iou_thrs, device=device)), dim=0)

    # shape = (n_recall_thresholds, nb_iou_thrs)
    sampled_precision_points = torch.gather(input=rolling_precisions, index=recall_threshold_idx, dim=0)

    # Average over the recall_thresholds
    ap = sampled_precision_points.mean(0)

    return ap, precision, recall, mean_f1_per_threshold, best_score_threshold
