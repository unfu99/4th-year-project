import json
import os
import os.path as osp
from glob import glob

import numpy as np
import pytorch3d
from PIL import Image

import ners.utils.image as image_util
from ners.utils import (
    compute_crop_parameters,
    compute_distance_transform,
    rle_to_binary_mask,
)


def get_bbox(img):
    a = np.where(img != 0)
    bbox = np.min(a[1]), np.min(a[0]), np.max(a[1]) + 1, np.max(a[0]) + 1
    return np.array(bbox)


def load_data_from_dir(instance_dir, image_size=256, pad_size=0.1, skip_indices=()):
    """
    Loads NeRS data from a directory. Assumes that a folder containing images and a
    folder container masks. Mask names should be the same as the images.
    """
    image_dir = osp.join(instance_dir, "images")
    mask_dir = osp.join(instance_dir, "masks")
    data_dict = {
        "images_og": [],
        "images": [],
        "masks": [],
        "masks_dt": [],
        "bbox": [],
        "image_centers": [],
        "crop_scales": [],
    }
    for i, image_path in enumerate(sorted(glob(osp.join(image_dir, "*.jpg")))):
        if i in skip_indices:
            continue
        image_name = osp.basename(image_path)
        mask_path = osp.join(mask_dir, image_name.replace("jpg", "png"))
        image_og = Image.open(image_path).convert("RGB")
        mask = Image.open(mask_path).convert("L")
        bbox = get_bbox(np.array(mask) / 255.0 > 0.5)
        center = (bbox[:2] + bbox[2:]) / 2.0
        s = max(bbox[2:] - bbox[:2]) / 2.0 * (1 + pad_size)
        square_bbox = np.concatenate([center - s, center + s]).astype(int)
        # Crop image and mask.
        image = image_util.crop_image(image_og, square_bbox)
        image = np.array(image.resize((image_size, image_size), Image.LANCZOS)) / 255.0
        mask = image_util.crop_image(mask, square_bbox)
        mask = np.array(mask.resize((image_size, image_size), Image.BILINEAR))
        mask = mask / 255.0 > 0.5
        image_center, crop_scale = compute_crop_parameters(image_og.size, square_bbox)
        data_dict["bbox"].append(square_bbox)
        data_dict["crop_scales"].append(crop_scale)
        data_dict["image_centers"].append(image_center)
        data_dict["images"].append(image)
        data_dict["images_og"].append(image_og)
        data_dict["masks"].append(mask)
        data_dict["masks_dt"].append(compute_distance_transform(mask))
    for k, v in data_dict.items():
        if k != "images_og":  # Original images can have any resolution.
            data_dict[k] = np.stack(v)

    if osp.exists(osp.join(instance_dir, "metadata.json")):
        metadata = json.load(open(osp.join(instance_dir, "metadata.json")))
        data_dict["extents"] = metadata["extents"]
        azimuths = metadata["azimuths"]
        elevations = metadata["elevations"]
        R, T = pytorch3d.renderer.look_at_view_transform(
            dist=2,
            elev=elevations,
            azim=azimuths,
        )
        data_dict["initial_poses"] = R.tolist()
    return data_dict


def load_car_data(
    data_dir, num_scenes=1, use_optimized_cameras=True, image_size=256, pad_size=0.1, reserve_first_image=True
):
    """
    Processes instance of car dataset for NeRS optimization.

    Args:
        instance_dir (str): Path to car instances.
        num_scene (int): Amount of scenes to load
        use_optimized_cameras (bool, optional): If true, uses optimized pose from NeRS.
            Otherwise, uses filtered poses from PoseFromShape.
        image_size (int, optional): Size of image crop.
        pad_size (float, optional): Amount to pad the bounding box before cropping.
        reserve_first_image (bool, optional): If true, exclude the first image from training and use it for evaluation.

    Returns:
        dict: Dictionary containing the following keys:
            "bbox": List of bounding boxes (xyxy).
            "crop_scales": List of crop scales.
            "image_centers": List of image centers.
            "images": List of cropped images.
            "images_og": List of original, uncropped images.
            "initial_poses": List of rotation matrices to initialize pose.
            "masks": List of binary masks.
            # also returns ids
    """
    instances_dirs = []
    dirs = os.listdir(data_dir)
    for num in range(0, num_scenes):
        dir = os.path.join(data_dir, dirs[num])
        instances_dirs.append(dir)

    data_dicts = []
    for dir in instances_dirs:
        annotations_json = osp.join(dir, "annotations.json")
        with open(annotations_json) as f:
            annotations = json.load(f)
        data_dict = {
            "id": int(annotations["id"]),
            "bbox": [],  # (N, 4).
            "crop_scales": [],  # (N,).
            "image_centers": [],  # (N, 2).
            "images": [],  # (N, 256, 256, 3).
            "images_og": [],  # (N, H, W, 3).
            "initial_poses": [],  # (N, 3, 3).
            "masks": [],  # (N, 256, 256).
            "masks_dt": [],  # (N, 256, 256).
        }
        for i in range(1 if reserve_first_image else 0, len(annotations["annotations"])):
            annotation = annotations["annotations"][i]
            filename = osp.join(dir, "images", annotation["filename"])

            # Make a square bbox.
            bbox = np.array(annotation["bbox"])
            center = ((bbox[:2] + bbox[2:]) / 2.0).astype(int)
            s = (max(bbox[2:] - bbox[:2]) / 2.0 * (1 + pad_size)).astype(int)
            square_bbox = np.concatenate([center - s, center + s])

            # Load image and mask.
            image_og = Image.open(filename).convert("RGB")
            mask = Image.fromarray(rle_to_binary_mask(annotation["mask"]))

            # Crop image and mask.
            image = image_util.crop_image(image_og, square_bbox)
            image = np.array(image.resize((image_size, image_size), Image.LANCZOS)) / 255.0
            mask = image_util.crop_image(mask, square_bbox)
            mask = np.array(mask.resize((image_size, image_size), Image.BILINEAR)) > 0.5
            image_center, crop_scale = compute_crop_parameters(image_og.size, square_bbox)
            if use_optimized_cameras:
                initial_pose = annotation["camera_optimized"]["R"]
            else:
                initial_pose = annotation["camera_initial"]["R"]
            data_dict["bbox"].append(square_bbox)
            data_dict["crop_scales"].append(crop_scale)
            data_dict["image_centers"].append(image_center)
            data_dict["images"].append(image)
            data_dict["images_og"].append(image_og)
            data_dict["initial_poses"].append(initial_pose)
            data_dict["masks"].append(mask)
            data_dict["masks_dt"].append(compute_distance_transform(mask))
        for k, v in data_dict.items():
            if k != "images_og" and k != "id":  # Original images can have any resolution.
                data_dict[k] = np.stack(v)
        data_dicts.append(data_dict)

    return data_dicts
