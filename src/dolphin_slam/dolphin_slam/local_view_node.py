#!/usr/bin/env python3
"""
优化的local_view_node.py - 专门为水下环境减少视觉更新频率
"""

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import Float32MultiArray
from cv_bridge import CvBridge
import numpy as np
import cv2
from typing import List, Optional
import time

class VisualTemplate:
    """视觉模板类"""
    def __init__(self, template_id: int, descriptors: np.ndarray, timestamp: float):
        self.template_id = template_id
        self.descriptors = descriptors
        self.timestamp = timestamp
        self.activation_count = 0
        self.last_activation = timestamp
        self.creation_time = timestamp
        self.temporal_weight = 1.0
        
    def update_activation(self, current_time: float):
        self.activation_count += 1
        self.last_activation = current_time

class LocalViewNode(Node):
    """优化的局部视觉细胞节点 - 减少视觉更新频率"""
    
    def __init__(self):
        super().__init__('local_view_node')
        
        # 声明参数
        self.declare_parameters(
            namespace='',
            parameters=[
                ('descriptors_topic', '/features/descriptors'),
                ('matches_topic', '/local_view/matches'),
                ('matching_algorithm', 'temporal_feature_matching'),
                ('similarity_threshold', 0.6),
                ('max_templates', 20),
                ('enable_debug', False),
                ('debug_level', 0),
                ('min_match_count', 15),
                ('match_ratio_threshold', 0.7),
                ('temporal_weight_factor', 5.0),
                ('recent_template_priority', 5),
                # 🔧 水下环境特定参数
                ('underwater_mode', True),
                ('frame_skip_threshold', 0.8),
                ('max_matches_per_second', 10),
                ('min_template_age', 3.0),                # 模板最小年龄
                ('significant_change_threshold', 0.15),   # 显著变化阈值
                ('temporal_smoothing_window', 5),         # 时间平滑窗口
            ]
        )
        
        # 获取参数
        self.descriptors_topic = self.get_parameter('descriptors_topic').value
        self.matches_topic = self.get_parameter('matches_topic').value
        self.similarity_threshold = self.get_parameter('similarity_threshold').value
        self.max_templates = self.get_parameter('max_templates').value
        self.enable_debug = self.get_parameter('enable_debug').value
        self.debug_level = self.get_parameter('debug_level').value
        self.min_match_count = self.get_parameter('min_match_count').value
        self.match_ratio_threshold = self.get_parameter('match_ratio_threshold').value
        self.temporal_weight_factor = self.get_parameter('temporal_weight_factor').value
        self.recent_template_priority = self.get_parameter('recent_template_priority').value
        
        # 水下环境参数
        self.underwater_mode = self.get_parameter('underwater_mode').value
        self.frame_skip_threshold = self.get_parameter('frame_skip_threshold').value
        self.max_matches_per_second = self.get_parameter('max_matches_per_second').value
        self.min_template_age = self.get_parameter('min_template_age').value
        self.significant_change_threshold = self.get_parameter('significant_change_threshold').value
        self.temporal_smoothing_window = self.get_parameter('temporal_smoothing_window').value
        
        # CV Bridge
        self.bridge = CvBridge()
        
        # 视觉模板库
        self.templates: List[VisualTemplate] = []
        self.template_counter = 0
        
        # 🔧 水下环境特定状态
        self.last_frame_time = 0
        self.last_similarity = 0.0
        self.similarity_history = []
        self.match_times = []
        self.frame_count = 0
        self.processed_frame_count = 0
        
        # 统计信息
        self.descriptor_count = 0
        self.match_count = 0
        self.successful_matches = 0
        self.skipped_frames = 0
        
        # 特征匹配器
        self.matcher = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
        
        # 订阅者
        self.descriptors_sub = self.create_subscription(
            Image,
            self.descriptors_topic,
            self.descriptors_callback,
            10
        )
        
        # 发布者
        self.match_pub = self.create_publisher(
            Float32MultiArray,
            self.matches_topic,
            10
        )
        
        self.get_logger().info(f'🌊 水下优化视觉节点启动')
        self.get_logger().info(f'水下模式: {self.underwater_mode}, 帧跳过阈值: {self.frame_skip_threshold}')
        self.get_logger().info(f'最大匹配率: {self.max_matches_per_second}/秒')
        
    def descriptors_callback(self, msg: Image):
        """处理描述符输入 - 添加帧跳过逻辑"""
        try:
            self.frame_count += 1
            current_time = time.time()
            
            # 🔧 水下环境帧跳过逻辑
            if self.underwater_mode and self._should_skip_frame(current_time):
                self.skipped_frames += 1
                return
            
            # 🔧 限制匹配频率
            if not self._check_match_rate_limit(current_time):
                return
            
            # 解码描述符
            descriptors = self._decode_descriptors(msg)
            if descriptors is None:
                return
            
            self.descriptor_count += 1
            self.processed_frame_count += 1
            
            # 执行匹配
            match_result = self._perform_matching(descriptors, current_time)
            
            # 🔧 只在显著变化时发布匹配结果
            if self._is_significant_change(match_result):
                self._publish_match_result(match_result)
                self.match_count += 1
                
                # 记录匹配时间
                self.match_times.append(current_time)
                if len(self.match_times) > 100:
                    self.match_times = self.match_times[-100:]
            
            # 更新统计
            self._update_statistics(match_result, current_time)
            
        except Exception as e:
            self.get_logger().error(f'描述符处理失败: {e}')
    
    def _should_skip_frame(self, current_time: float) -> bool:
        """判断是否应该跳过当前帧"""
        
        # 时间间隔检查
        if current_time - self.last_frame_time < 0.1:  # 最小间隔100ms
            return True
        
        # 🔧 基于相似度历史的跳过逻辑
        if len(self.similarity_history) >= 2:
            recent_similarities = self.similarity_history[-2:]
            if all(sim > self.frame_skip_threshold for sim in recent_similarities):
                # 如果最近的帧都很相似，跳过更多帧
                return np.random.random() < 0.7  # 70%概率跳过
        
        return False
    
    def _check_match_rate_limit(self, current_time: float) -> bool:
        """检查匹配率限制"""
        # 清理旧的匹配记录
        cutoff_time = current_time - 1.0  # 1秒窗口
        self.match_times = [t for t in self.match_times if t > cutoff_time]
        
        # 检查是否超过限制
        if len(self.match_times) >= self.max_matches_per_second:
            return False
        
        return True
    
    def _decode_descriptors(self, msg: Image) -> Optional[np.ndarray]:
        """解码描述符"""
        try:
            # 假设描述符以图像格式传输
            cv_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
            
            # 检查是否为有效的描述符数据
            if cv_image.dtype != np.float32:
                cv_image = cv_image.astype(np.float32)
            
            # 重塑为描述符格式
            if len(cv_image.shape) == 3:
                descriptors = cv_image.reshape(-1, cv_image.shape[-1])
            else:
                descriptors = cv_image
            
            return descriptors
            
        except Exception as e:
            if self.debug_level >= 2:
                self.get_logger().error(f'描述符解码失败: {e}')
            return None
    
    def _perform_matching(self, descriptors: np.ndarray, current_time: float) -> dict:
        """执行匹配"""
        
        # 如果没有模板，创建第一个
        if len(self.templates) == 0:
            return self._create_new_template(descriptors, current_time)
        
        # 🔧 只检查成熟的模板
        mature_templates = [t for t in self.templates 
                           if current_time - t.creation_time > self.min_template_age]
        
        if not mature_templates:
            return self._create_new_template(descriptors, current_time)
        
        # 匹配逻辑
        best_match_id = -1
        best_similarity = 0.0
        best_match_count = 0
        
        # 优先检查最近的模板
        recent_templates = mature_templates[-self.recent_template_priority:]
        
        for template in reversed(recent_templates):
            try:
                similarity, match_count = self._safe_feature_matching(descriptors, template.descriptors)
                
                # 应用时间权重
                weighted_similarity = similarity * (1.0 + self.temporal_weight_factor * template.temporal_weight)
                
                if weighted_similarity > best_similarity:
                    best_similarity = weighted_similarity
                    best_match_id = template.template_id
                    best_match_count = match_count
                    
            except Exception as e:
                continue
        
        # 判断匹配成功
        if best_similarity > self.similarity_threshold and best_match_count >= self.min_match_count:
            self.successful_matches += 1
            matched_template = next(t for t in self.templates if t.template_id == best_match_id)
            matched_template.update_activation(current_time)
            
            return {
                'matched': True,
                'template_id': best_match_id,
                'similarity': best_similarity,
                'match_count': best_match_count,
                'is_novel': False
            }
        else:
            # 🔧 更严格的新模板创建条件
            if (best_similarity < self.similarity_threshold * 0.5 and 
                len(self.templates) < self.max_templates):
                return self._create_new_template(descriptors, current_time)
            else:
                return {
                    'matched': False,
                    'template_id': -1,
                    'similarity': best_similarity,
                    'match_count': best_match_count,
                    'is_novel': False
                }
    
    def _safe_feature_matching(self, desc1: np.ndarray, desc2: np.ndarray) -> tuple:
        """安全的特征匹配"""
        try:
            if desc1.shape[0] < 2 or desc2.shape[0] < 2:
                return 0.0, 0
            
            # 使用KNN匹配
            matches = self.matcher.knnMatch(desc1, desc2, k=2)
            
            # 比率测试
            good_matches = []
            for match_pair in matches:
                if len(match_pair) == 2:
                    m, n = match_pair
                    if m.distance < self.match_ratio_threshold * n.distance:
                        good_matches.append(m)
            
            if len(good_matches) == 0:
                return 0.0, 0
            
            # 计算相似度
            distances = [m.distance for m in good_matches]
            avg_distance = np.mean(distances)
            similarity = max(0.0, 1.0 - avg_distance / 256.0)  # 归一化
            
            return similarity, len(good_matches)
            
        except Exception as e:
            return 0.0, 0
    
    def _create_new_template(self, descriptors: np.ndarray, current_time: float) -> dict:
        """创建新模板"""
        template = VisualTemplate(self.template_counter, descriptors.copy(), current_time)
        self.templates.append(template)
        self.template_counter += 1
        
        # 限制模板数量
        if len(self.templates) > self.max_templates:
            self.templates.pop(0)
        
        return {
            'matched': True,
            'template_id': template.template_id,
            'similarity': 1.0,
            'match_count': descriptors.shape[0],
            'is_novel': True
        }
    
    def _is_significant_change(self, match_result: dict) -> bool:
        """判断是否为显著变化"""
        
        current_similarity = match_result['similarity']
        
        # 更新相似度历史
        self.similarity_history.append(current_similarity)
        if len(self.similarity_history) > self.temporal_smoothing_window:
            self.similarity_history = self.similarity_history[-self.temporal_smoothing_window:]
        
        # 🔧 显著变化检测
        if len(self.similarity_history) < 2:
            return True  # 前几帧总是发布
        
        # 计算变化幅度
        recent_avg = np.mean(self.similarity_history[-3:]) if len(self.similarity_history) >= 3 else current_similarity
        change_magnitude = abs(current_similarity - recent_avg)
        
        # 新模板总是显著的
        if match_result.get('is_novel', False):
            return True
        
        # 高质量匹配或显著变化
        if (current_similarity > self.similarity_threshold * 1.2 or 
            change_magnitude > self.significant_change_threshold):
            return True
        
        return False
    
    def _publish_match_result(self, match_result: dict):
        """发布匹配结果"""
        match_msg = Float32MultiArray()
        match_msg.data = [
            float(match_result['similarity']),
            float(match_result['template_id']),
            float(1.0 if match_result['matched'] else 0.0),
            float(1.0 if match_result.get('is_novel', False) else 0.0)
        ]
        
        self.match_pub.publish(match_msg)
    
    def _update_statistics(self, match_result: dict, current_time: float):
        """更新统计信息"""
        self.last_frame_time = current_time
        self.last_similarity = match_result['similarity']
        
        # 定期报告
        if self.processed_frame_count % 100 == 0:
            skip_rate = self.skipped_frames / self.frame_count if self.frame_count > 0 else 0
            match_rate = self.match_count / self.processed_frame_count if self.processed_frame_count > 0 else 0
            
            self.get_logger().info(
                f'🌊 处理统计: 总帧数={self.frame_count}, '
                f'处理帧数={self.processed_frame_count}, '
                f'跳过率={skip_rate:.1%}, '
                f'匹配率={match_rate:.1%}, '
                f'模板数={len(self.templates)}'
            )

def main(args=None):
    rclpy.init(args=args)
    
    try:
        node = LocalViewNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == '__main__':
    main()